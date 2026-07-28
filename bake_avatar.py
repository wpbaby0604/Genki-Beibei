#!/usr/bin/env python3
# ============================================================
#  bake_avatar_liveportrait.py — 用 LivePortrait 取代 THA3 的預烤腳本
#
#  這支腳本是 bake_avatar.py 的「換引擎版」：
#    - 輸入 / 輸出的 .json 結構完全相同（version / gridDim / clips / grid），
#      companion_live2d.py 不需要改任何一行就能吃這支腳本烤出來的檔案。
#    - 差異只在「誰來畫每一幀」：THA3 用 45 維姿態向量控制動漫圖，
#      這支改用 LivePortrait 官方 Gradio pipeline 裡的
#      execute_image_retargeting()（單張相片 + 一組具名滑桿參數 → 一幀），
#      對「真人照片」是官方主打情境，效果理論上會比 THA3 自然很多。
#
#  重要差異（請務必知道，不是 bug）：
#    1. THA3 輸出是「去背 RGBA 動漫貼圖」；LivePortrait 是「真人照片 paste-back」，
#       輸出幀「沒有透明背景」，會連同你上傳照片本身的背景一起動起來。
#       如果你的前端假設頭像是去背貼圖疊在 bg.png 上，這裡出來的效果會是
#       「整張照片在動」而不是「一個角色浮在背景上」。這是預期行為，
#       不是這支腳本的錯誤——要不要接受這種「會動的照片」質感、或额外加一道
#       去背（例如 rembg）讓它變回貼圖式，是你們要做的產品決策，不在這支
#       腳本的範圍內。
#    2. 下面每個動作的參數包絡（idle/yawn/alert/talk_*）是我依照 LivePortrait
#       官方 Gradio demo 滑桿的合法數值範圍「類比」THA3 原本的 45 維姿態設計
#       手動抓出來的合理起始值，不是任何論文或官方案例的標準答案。
#       實際效果請你們自己烤出來看過再調整下面 *_ENVELOPE 常數，
#       這是「探索原型」，不是最終定案。
#
#  使用方式（在 Colab 或本地、且已 clone LivePortrait + 下載好權重的地方執行）：
#       python bake_avatar_liveportrait.py 我的照片.jpg 貝貝 --lp-root /content/LivePortrait
#  產出：baked_avatars/貝貝.json
# ============================================================
import io
import os
import sys
import json
import math
import base64
import argparse

try:
    import numpy as np
    from PIL import Image
except Exception as e:
    print("✗ 缺少基本套件（numpy / pillow）。原始錯誤：", e)
    sys.exit(1)


ANIM_VERSION = "v3-lp"          # 跟 THA3 版的 "v3" 分開命名，方便日後追溯是哪個引擎烤的
OUTPUT_SIZE = 512               # 統一 resize 成 512x512，跟現有前端的顯示假設一致

# ---- 跟 bake_avatar.py 完全相同的時間軸設定，讓兩邊烤出來的動畫長度一致 ----
ACTION_FRAMES = {
    "idle":         (50, 110),
    "yawn":         (28, 110),
    "alert":        (40, 120),
    "talk_neutral": (24, 90),
    "talk_happy":   (24, 90),
    "talk_sad":     (24, 90),
    "talk_angry":   (24, 90),
}

GRID_DIM = 7
GRID_YAW_RANGE_DEG = 20.0        # 對應 THA3 GRID_HEAD_X_RANGE 的角度類比值（未精確校準，見檔頭說明）
GRID_PITCH_RANGE_DEG = 15.0      # 對應 THA3 GRID_HEAD_Y_RANGE 的角度類比值

# 用來模擬「說話嘴型變化」的一組粗略 viseme 預設
# (lip_ratio, lip_variation_one, lip_variation_two, lip_variation_three)
VISEME_PRESETS = [
    (0.55,   0.0,  0.0,  40.0),   # 類似「啊」：張大
    (0.25,  -5.0,  3.0,  10.0),   # 類似「衣」：扁而略開
    (0.20,   8.0,  0.0, -10.0),   # 類似「烏」：嘟起
    (0.45,   0.0,  0.0,  25.0),   # 類似「歐」：圓而開
    (0.30,  -3.0,  5.0,  15.0),   # 類似「欸」：中等偏開
]


def _lerp(a, b, t):
    return a + (b - a) * t


def _apply_expression_overlay(params, expr):
    """疊加喜怒哀樂到基礎說話參數上（近似 THA3 的 _apply_expression）。
    數值已加大到接近滑桿合法範圍的顯著比例，確保表情肉眼可辨，
    不是只做出微弱、容易被忽略的變化。"""
    if expr == "happy":
        params["smile"] = 1.1                    # 範圍 -0.3~1.3，取接近上限的燦笑
        params["eyebrow"] += 15.0                 # 範圍 ±30，明顯上揚
        params["input_eye_ratio"] = min(params["input_eye_ratio"] * 1.15, 0.8)  # 眼睛稍微睜大，更有神
    elif expr == "sad":
        params["smile"] = -0.3                    # 取下限，明顯的嘴角下垂
        params["eyebrow"] -= 18.0                 # 明顯的八字眉
        params["input_head_pitch_variation"] -= 6.0   # 頭更明顯地低下去
        params["input_eye_ratio"] *= 0.8          # 眼神比較沒精神
    elif expr == "angry":
        params["smile"] = -0.25
        params["eyebrow"] -= 25.0                 # 接近下限，明顯皺眉
        params["input_eye_ratio"] *= 0.55         # 明顯瞇眼
        params["lip_variation_two"] = params.get("lip_variation_two", 0.0) + 6.0  # 抿嘴/咬牙感
    return params


def _build_lp_params(action, t, i, n, source_eye_ratio, source_lip_ratio):
    """
    LivePortrait 版的 _build_pose：回傳 execute_image_retargeting 需要的
    一整組具名參數（而不是 THA3 的 45 維向量）。
    """
    p = dict(
        input_eye_ratio=source_eye_ratio,
        input_lip_ratio=source_lip_ratio,
        input_head_pitch_variation=0.0,
        input_head_yaw_variation=0.0,
        input_head_roll_variation=0.0,
        mov_x=0.0, mov_y=0.0, mov_z=1.0,
        lip_variation_zero=0.0, lip_variation_one=0.0,
        lip_variation_two=0.0, lip_variation_three=0.0,
        smile=0.0, wink=0.0, eyebrow=0.0,
        eyeball_direction_x=0.0, eyeball_direction_y=0.0,
    )

    if action == "idle":
        # 呼吸/頭部微晃：用小幅度 pitch/yaw sin 波近似 THA3 的 breathing+sway
        p["input_head_pitch_variation"] = 1.2 * math.sin(2 * math.pi * t)
        p["input_head_yaw_variation"] = 2.0 * math.sin(2 * math.pi * t + 1.0)
        # 眨眼：在週期 82% 附近眨一下（跟 THA3 的 center=int(n*0.82) 邏輯一致）
        center = int(n * 0.82)
        d = abs(i - center)
        if d <= 2:
            close_amount = max(0.0, 1.0 - d / 3.0) ** 1.5
            p["input_eye_ratio"] = _lerp(source_eye_ratio, 0.03, close_amount)

    elif action == "yawn":
        c = math.sin(math.pi * t)
        p["input_lip_ratio"] = _lerp(source_lip_ratio, 0.75, c)
        p["input_eye_ratio"] = _lerp(source_eye_ratio, 0.05, c)
        p["eyebrow"] = 20.0 * c
        p["input_head_pitch_variation"] = -5.0 * c

    elif action == "alert":
        p["input_head_yaw_variation"] = 10.0 * math.sin(t * 6 * math.pi)
        p["input_eye_ratio"] = 0.75
        p["eyebrow"] = 27.0
        p["input_lip_ratio"] = 0.38   # 明顯張大的「啊！」嘴形，不是只微微張開

    elif action.startswith("talk"):
        preset = VISEME_PRESETS[(i // 2) % len(VISEME_PRESETS)]
        p["input_lip_ratio"] = preset[0]
        p["lip_variation_one"] = preset[1]
        p["lip_variation_two"] = preset[2]
        p["lip_variation_three"] = preset[3]
        p["input_head_pitch_variation"] = 3.0 * math.sin(2 * math.pi * t)
        p["eyebrow"] = 6.0 * math.sin(2 * math.pi * t * 2)

        if action == "talk_happy":
            _apply_expression_overlay(p, "happy")
        elif action == "talk_sad":
            _apply_expression_overlay(p, "sad")
        elif action == "talk_angry":
            _apply_expression_overlay(p, "angry")

    return p


def _to_pil(rgb_uint8_hw3):
    return Image.fromarray(rgb_uint8_hw3, "RGB").resize((OUTPUT_SIZE, OUTPUT_SIZE))


def _bake_clip(pipeline, photo_path, source_eye_ratio, source_lip_ratio, action, scale):
    num_frames, frame_ms = ACTION_FRAMES.get(action, (40, 120))
    frames = []
    for i in range(num_frames):
        t = i / num_frames
        params = _build_lp_params(action, t, i, num_frames, source_eye_ratio, source_lip_ratio)
        _, out_blend = pipeline.execute_image_retargeting(
            input_eye_ratio=params["input_eye_ratio"],
            input_lip_ratio=params["input_lip_ratio"],
            input_head_pitch_variation=params["input_head_pitch_variation"],
            input_head_yaw_variation=params["input_head_yaw_variation"],
            input_head_roll_variation=params["input_head_roll_variation"],
            mov_x=params["mov_x"], mov_y=params["mov_y"], mov_z=params["mov_z"],
            lip_variation_zero=params["lip_variation_zero"],
            lip_variation_one=params["lip_variation_one"],
            lip_variation_two=params["lip_variation_two"],
            lip_variation_three=params["lip_variation_three"],
            smile=params["smile"], wink=params["wink"], eyebrow=params["eyebrow"],
            eyeball_direction_x=params["eyeball_direction_x"],
            eyeball_direction_y=params["eyeball_direction_y"],
            input_image=photo_path,
            retargeting_source_scale=scale,
            flag_stitching_retargeting_input=True,
            flag_do_crop_input_retargeting_image=True,
        )
        frames.append(_to_pil(out_blend))
        if (i + 1) % 10 == 0 or i == num_frames - 1:
            print(f"    幀 {i + 1}/{num_frames}")

    buf = io.BytesIO()
    frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:],
                    duration=frame_ms, loop=0, quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _bake_grid(pipeline, photo_path, source_eye_ratio, source_lip_ratio, scale):
    N = GRID_DIM
    yaws = [(-GRID_YAW_RANGE_DEG + 2 * GRID_YAW_RANGE_DEG * (c / (N - 1))) for c in range(N)]
    pitches = [(-GRID_PITCH_RANGE_DEG + 2 * GRID_PITCH_RANGE_DEG * (r / (N - 1))) for r in range(N)]
    frames_b64 = []
    for pitch in pitches:            # row-major：外層列(y/pitch)、內層行(x/yaw)，跟 THA3 版一致
        for yaw in yaws:
            _, out_blend = pipeline.execute_image_retargeting(
                input_eye_ratio=source_eye_ratio,
                input_lip_ratio=source_lip_ratio,
                input_head_pitch_variation=pitch,
                input_head_yaw_variation=yaw,
                input_head_roll_variation=0.0,
                mov_x=0.0, mov_y=0.0, mov_z=1.0,
                lip_variation_zero=0.0, lip_variation_one=0.0,
                lip_variation_two=0.0, lip_variation_three=0.0,
                smile=0.0, wink=0.0, eyebrow=0.0,
                eyeball_direction_x=0.0, eyeball_direction_y=0.0,
                input_image=photo_path,
                retargeting_source_scale=scale,
                flag_stitching_retargeting_input=True,
                flag_do_crop_input_retargeting_image=True,
            )
            frame = _to_pil(out_blend)
            fbuf = io.BytesIO()
            frame.save(fbuf, format="WEBP", quality=80)
            frames_b64.append(base64.b64encode(fbuf.getvalue()).decode("utf-8"))
    return frames_b64


def _load_pipeline(lp_root):
    """把 LivePortrait 的 src/ 掛進 sys.path 並建立 GradioPipeline。"""
    sys.path.insert(0, lp_root)
    os.chdir(lp_root)   # LivePortrait 內部大量用相對路徑找權重，穩妥起見切過去

    from src.gradio_pipeline import GradioPipeline
    from src.config.argument_config import ArgumentConfig
    from src.config.inference_config import InferenceConfig
    from src.config.crop_config import CropConfig

    args = ArgumentConfig()

    def partial_fields(target_class, kwargs):
        return target_class(**{k: v for k, v in kwargs.items() if hasattr(target_class, k)})

    inference_cfg = partial_fields(InferenceConfig, args.__dict__)
    crop_cfg = partial_fields(CropConfig, args.__dict__)

    print("  → 載入 LivePortrait 模型中…")
    pipeline = GradioPipeline(inference_cfg=inference_cfg, crop_cfg=crop_cfg, args=args)
    return pipeline


def bake_payload(pipeline, photo_path: str, scale: float = 2.3, log=print) -> dict:
    """
    核心烤製邏輯，回傳 payload dict（不寫檔）。
    給 CLI 版 bake() 和佇列版 worker 共用，模型只需在外面 _load_pipeline() 一次，
    這個函式可以對同一個 pipeline 重複呼叫、連續烤很多張不同照片。
    """
    photo_path = os.path.abspath(photo_path)
    if not os.path.exists(photo_path):
        raise FileNotFoundError(f"找不到照片：{photo_path}")

    log(f"  來源相片：{photo_path}")
    src_eye_ratio, src_lip_ratio = pipeline.init_retargeting_image(
        retargeting_source_scale=scale, source_eye_ratio=0.4, source_lip_ratio=0.0,
        input_image=photo_path,
    )
    log(f"  預設眼睛開合度={src_eye_ratio}, 嘴巴開合度={src_lip_ratio}")

    clips = {}
    actions = ["idle", "yawn", "alert", "talk_neutral", "talk_happy", "talk_sad", "talk_angry"]
    for idx, a in enumerate(actions, 1):
        log(f"  [{idx}/{len(actions)}] 烤製動作：{a} …")
        clips[a] = _bake_clip(pipeline, photo_path, src_eye_ratio, src_lip_ratio, a, scale)

    log(f"  烤製臉部同步角度網格 {GRID_DIM}×{GRID_DIM}（{GRID_DIM * GRID_DIM} 張,較耗時)…")
    grid = _bake_grid(pipeline, photo_path, src_eye_ratio, src_lip_ratio, scale)

    return {
        "version": ANIM_VERSION,
        "gridDim": GRID_DIM,
        "clips": clips,
        "grid": grid,
    }


def bake(photo_path: str, out_name: str, lp_root: str, scale: float = 2.3):
    """CLI 版：單張照片 → 本地 baked_avatars/<名字>.json（Step 5 notebook 用的就是這支）。"""
    project_root = os.getcwd()
    pipeline = _load_pipeline(lp_root)

    print(f"開始烤製「{out_name}」")
    payload = bake_payload(pipeline, photo_path, scale, log=print)

    os.chdir(project_root)
    os.makedirs("baked_avatars", exist_ok=True)
    out_path = os.path.join("baked_avatars", f"{out_name}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"✓ 完成！已存成 {out_path}（{size_mb:.1f} MB）")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("photo")
    parser.add_argument("name", nargs="?", default=None)
    parser.add_argument("--lp-root", default="/content/LivePortrait",
                         help="LivePortrait repo 的路徑（Colab 預設 clone 到這裡）")
    parser.add_argument("--scale", type=float, default=2.3,
                         help="人臉裁切縮放比例，對應官方 Gradio 的 retargeting_source_scale")
    args = parser.parse_args()

    name = args.name or os.path.splitext(os.path.basename(args.photo))[0]
    bake(args.photo, name, args.lp_root, args.scale)
