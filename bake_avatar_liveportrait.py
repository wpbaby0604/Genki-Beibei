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
#       原生輸出幀「沒有透明背景」，會連同你上傳照片本身的背景（拍照時的牆壁/房間）
#       一起動起來。為了讓角色像「一個人浮在前端背景上」而不是「一整張含背景的照片
#       在動」，本腳本在每一幀 LivePortrait 輸出後，加了一道 rembg 人像去背
#       （見下方 _remove_bg / REMOVE_BACKGROUND）：把人像以外挖成透明，再進 _to_pil
#       做等比例縮放 + 置中 letterbox。最終一張幀的透明度分三層：
#         人像 = 不透明；原照片背景 = 透明（rembg 挖掉）；letterbox 補邊 = 透明。
#       三者融為一致的透明背景，前端換背景永遠正確、不用重烤。
#       去背相關注意事項：
#         - rembg 需裝進 worker 的隔離 venv（見檔尾「worker venv 安裝」註解），
#           模型 session 用全域快取只載入一次（_get_rembg_session），不會每幀重載。
#         - 若 venv 內沒有 rembg、或去背中途失敗，本腳本「不會中斷烤製」：
#           會印一次警告並自動退回舊行為（人像不透明、只有 letterbox 邊透明），
#           avatar 照樣烤得出來（只是背景沒去掉）。可用 --no-rembg 主動關閉做 A/B。
#         - 去背品質先天有限（髮絲邊緣、與背景同色的衣服），且逐幀獨立分割可能
#           造成邊緣輕微閃爍；屬已知限制，烤完請用 Cell 6 表情總覽實看再決定微調。
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

# ---- rembg 人像去背設定（把原照片背景挖成透明，只留人）----------------------
# 這幾個常數是「經驗近似 + 可調」，不是理論校準；改了要重烤才生效。
REMOVE_BACKGROUND = True        # 總開關。CLI 可用 --no-rembg 覆蓋；worker 走這個預設值。
REMBG_MODEL = "u2net_human_seg" # 專為「人像分割」訓練的模型，對真人照片比通用模型穩。
                                #   備選："isnet-general-use"（邊緣/髮絲較細但較慢、較吃記憶體）
                                #         "u2net"（通用，速度中庸）
REMBG_ALPHA_MATTING = False     # True 會多跑一道 alpha matting，明顯改善髮絲邊緣，
                                #   但每幀慢很多（479 幀會很有感）。先預設關，需要再開來 A/B。
REMBG_AM_FG_THRESHOLD = 240     # 下面三個只有 alpha_matting=True 時才作用
REMBG_AM_BG_THRESHOLD = 15
REMBG_AM_ERODE_SIZE = 3

_REMBG_SESSION = None           # 全域快取：模型只在第一次去背時載入一次，之後每幀重用
_REMBG_DISABLED = False         # 首次失敗（沒裝/載入錯）後就整輪停用，避免每幀噴同樣的錯
_REMBG_STATS = {"frames": 0, "seconds": 0.0}   # 累計去背耗時，供評估「可接受程度」

# ---- 跟 bake_avatar.py 完全相同的時間軸設定，讓兩邊烤出來的動畫長度一致 ----
ACTION_FRAMES = {
    "idle":         (50, 110),
    "idle_tilt":    (40, 110),   # 🌟 新增：偏頭一下（一去一回）
    "idle_glance":  (36, 110),   # 🌟 新增：視線飄一下（眼睛動、頭幾乎不動）
    "idle_glance2": (36, 110),   # 🌟 新增：飄眼(另一側)，前端隨機挑左/右
    "idle_nod":     (32, 110),   # 🌟 新增：微微點頭
    "yawn":         (28, 110),
    "alert":        (40, 120),
    "talk_neutral": (24, 90),
    "talk_happy":   (24, 90),
    "talk_sad":     (24, 90),
    "talk_angry":   (24, 90),
}

# ---- idle 眨眼時間點：一輪在 82% 附近眨一次（原始頻率）----

GRID_DIM = 11             # 從 7 加大到 11：格子切更細，每格角度差變小，跳格子的頓挫感會降低
                          # （代價：網格從 49 張變 121 張，烤製時間變長、json 檔案變大）
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
    """疊加喜怒哀樂到基礎說話參數上。
    針對真人臉大幅拉高幅度——真人臉比動漫圖需要更誇張的參數才看得出情緒差異，
    而且情緒相關參數（smile/eyebrow/eye）改成「直接設定固定值」而非在 sin 波動上加成，
    避免說話時的頭部/眉毛擺動把情緒稀釋掉。"""
    if expr == "happy":
        params["smile"] = 0.7                      # 從 1.3 降到 0.7：真人臉 1.3 會用力過猛、變成吼叫感，
                                                   # 0.7 是「帶著笑意」的自然微笑幅度
        params["eyebrow"] = 12.0                   # 眉毛微微上揚即可（原本 22 太挑，顯得驚訝而非開心）
        params["input_eye_ratio"] = max(params["input_eye_ratio"] * 0.82, 0.18)  # 眼睛微瞇——
                                                   # 真人「開心笑」時眼睛是瞇起來的（杜鄉式微笑），不是睜大
        params["_happy_mouth_damp"] = True         # 標記：開心說話時把嘴巴張開幅度收斂一點（見下方 talk 區塊）
    elif expr == "sad":
        # 使用者選定：沿用原「臭臉」那組(微皺眉 + 平嘴 + 垂眼)，頭朝正前方，作為難過表情。
        params["smile"] = -0.3                      # 嘴角微下垂/平
        params["eyebrow"] = -10.0                   # 小幅皺眉(往下壓)
        params["input_eye_ratio"] *= 0.5            # 眼睛半垂
        params["input_head_pitch_variation"] = 0.0      # 臉朝正前方
        params["input_head_roll_variation"] = 0.0       # 不歪頭
    elif expr == "angry":
        # angry 的辨識度靠「強皺眉 + 銳利的瞪視 + 抿嘴」。
        # 關鍵：眼睛不再縮小，而是維持正常/略大，做出「瞪」的銳利感，
        # 這樣才跟 sad 的半垂眼形成明確對比（讓眼睛狀態承擔 sad/angry 的區別）。
        params["smile"] = -0.35                   # 嘴角下壓（略強於 sad）
        params["eyebrow"] = -30.0                 # 下限，最強皺眉（跟 sad 的 -10 拉開）
        params["input_eye_ratio"] = min(params["input_eye_ratio"] * 1.1, 0.8)  # 正常/略大＝瞪視
        params["input_head_pitch_variation"] = 2.0    # 略抬下巴，帶挑釁感（與 sad 低頭相反）
        params["lip_variation_two"] = params.get("lip_variation_two", 0.0) + 10.0  # 抿嘴/咬牙
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
        # 眨眼：一輪在 82% 附近眨一下（原始頻率，一輪一次，自然不誇張）。
        center = int(n * 0.82)
        d = abs(i - center)
        if d <= 2:
            close_amount = max(0.0, 1.0 - d / 3.0) ** 1.5
            p["input_eye_ratio"] = _lerp(source_eye_ratio, 0.03, close_amount)

    elif action == "idle_tilt":
        # 偏頭一下：頭慢慢歪向一側再回正。用半個 sin（0→1→0）確保「起點=終點=正臉」，
        # 這樣任何一段接在別段前後都不會跳臉，方便前端隨機穿插。
        c = math.sin(math.pi * t)                          # 0 → 1 → 0，一去一回
        p["input_head_roll_variation"] = 6.0 * c           # 最多歪 6 度
        p["input_head_yaw_variation"] = 3.0 * c            # 順帶一點點轉向，更自然
        p["input_head_pitch_variation"] = 1.0 * math.sin(2 * math.pi * t)  # 保留呼吸微晃
        if abs(i - int(n * 0.5)) <= 2:                     # 中段輕眨一下，避免死盯
            p["input_eye_ratio"] = _lerp(source_eye_ratio, 0.03, 1.0 - abs(i - int(n * 0.5)) / 3.0)

    elif action == "idle_glance":
        # 視線飄一下：眼睛看向一側再收回，頭幾乎不動。眼睛會動＝很有生命感。
        c = math.sin(math.pi * t)
        p["eyeball_direction_x"] = 12.0 * c                # 眼球往一側看：0.6 幾乎看不到，大幅拉高到 12（範圍約 ±15，待實測微調）
        p["input_head_yaw_variation"] = 0.5 * math.sin(2 * math.pi * t)    # 頭幾乎不動，讓「眼睛動」當主角
        p["input_head_pitch_variation"] = 1.0 * math.sin(2 * math.pi * t)  # 呼吸

    elif action == "idle_glance2":
        # 飄眼(另一側)：跟 idle_glance 相同，但眼球往相反方向 → 前端隨機挑左/右，做出「每次不一定」。
        c = math.sin(math.pi * t)
        p["eyeball_direction_x"] = -12.0 * c               # 往相反側看
        p["input_head_yaw_variation"] = -0.5 * math.sin(2 * math.pi * t)
        p["input_head_pitch_variation"] = 1.0 * math.sin(2 * math.pi * t)

    elif action == "idle_nod":
        # 微微點頭：頭往下點一下再回來。上下幅度加大、左右晃減小。
        c = math.sin(math.pi * t)
        p["input_head_pitch_variation"] = -8.0 * c         # 負=低頭，上下點頭幅度加大(原本 -4)
        p["input_head_yaw_variation"] = 0.3 * math.sin(2 * math.pi * t)    # 左右晃減小(原本 1.0)
        if abs(i - int(n * 0.5)) <= 2:                     # 點到最低時順勢眨一下
            p["input_eye_ratio"] = _lerp(source_eye_ratio, 0.03, 1.0 - abs(i - int(n * 0.5)) / 3.0)

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

        # 開心說話：把說話張嘴的幅度收斂到約 65%，避免大張的嘴把「笑意」蓋掉，
        # 讓效果更接近「帶著微笑講話」而不是「用力張大嘴」。
        if p.pop("_happy_mouth_damp", False):
            p["input_lip_ratio"] *= 0.65

        # 第 0 幀強制閉嘴：這一幀是「表情總覽」抓來當代表的畫面，
        # 閉嘴才能讓嘴角的笑/垂、眉毛的情緒清楚呈現，不會被說話張大的嘴蓋掉。
        # （後續幀正常做視位循環，動畫播放時嘴巴照樣會動。）
        if i == 0:
            if action == "talk_neutral":
                # 中性說話：保留一點半張嘴，跟閉嘴的 idle 區隔開來
                p["input_lip_ratio"] = 0.12
            else:
                p["input_lip_ratio"] = 0.0
            p["lip_variation_one"] = 0.0
            p["lip_variation_two"] = p.get("lip_variation_two", 0.0)  # 保留 angry 的抿嘴
            p["lip_variation_three"] = 0.0

    return p


def _get_rembg_session():
    """全域快取的 rembg session：模型只載入一次，之後每幀重用（每幀重載會非常慢）。"""
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session
        try:
            # 優先用 GPU(CUDA)，抓不到就自動退回 CPU。worker 在 Colab GPU 上會走 CUDA。
            _REMBG_SESSION = new_session(
                REMBG_MODEL,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
        except TypeError:
            # 舊版 rembg 的 new_session() 不接受 providers 參數
            _REMBG_SESSION = new_session(REMBG_MODEL)
        print(f"  → rembg 去背已啟用（模型：{REMBG_MODEL}"
              f"{'，alpha_matting=on' if REMBG_ALPHA_MATTING else ''}）")
    return _REMBG_SESSION


def _remove_bg(img):
    """把單幀（LivePortrait paste-back 的 RGB 影像）的原始背景挖成透明，只留人像，回傳 RGBA。

    安全設計：只要去背失敗（沒裝 rembg / 模型載入失敗 / 推論出錯），
    「絕不中斷整輪烤製」——印一次警告、把 _REMBG_DISABLED 打開，之後所有幀
    都安靜地退回舊行為（img 轉成不透明 RGBA，只有外圈 letterbox 透明）。
    這樣即使 worker venv 還沒裝好 rembg，avatar 照樣烤得出來（只是背景沒去掉）。"""
    global _REMBG_DISABLED
    if not REMOVE_BACKGROUND or _REMBG_DISABLED:
        return img.convert("RGBA")
    try:
        import time
        from rembg import remove
        session = _get_rembg_session()
        t0 = time.time()
        cut = remove(
            img,
            session=session,
            alpha_matting=REMBG_ALPHA_MATTING,
            alpha_matting_foreground_threshold=REMBG_AM_FG_THRESHOLD,
            alpha_matting_background_threshold=REMBG_AM_BG_THRESHOLD,
            alpha_matting_erode_size=REMBG_AM_ERODE_SIZE,
        )
        _REMBG_STATS["frames"] += 1
        _REMBG_STATS["seconds"] += time.time() - t0
        return cut.convert("RGBA")   # 確保是 RGBA（remove 對 RGB 輸入本就回 RGBA，保險起見）
    except Exception as e:
        _REMBG_DISABLED = True
        print(f"  ⚠ rembg 去背失敗，本輪改用『不去背』（人像不透明、只有 letterbox 邊透明）。"
              f"原因：{e}")
        return img.convert("RGBA")


def _to_pil(rgb_uint8_hw3):
    """把 LivePortrait 輸出的畫面轉成 512x512 的 PIL 影像（RGBA）。

    流程：原始幀 → 等比例縮放到 ≤512（避免非正方形被壓扁；先縮小再去背，
    把 rembg 的工作量固定在顯示解析度、速度可預期）→ rembg 人像去背（背景挖成透明）
    → 置中貼到 512×512 全透明畫布。

    最終透明度三層：人像=不透明；原照片背景=透明（rembg）；letterbox 補邊=透明。
    去背關閉/失敗時，_remove_bg 會退回『不透明人像』，此時仍只有 letterbox 邊透明
    （＝這支腳本加去背之前的舊行為），前端不會壞。"""
    img = Image.fromarray(rgb_uint8_hw3, "RGB")
    w, h = img.size
    scale_ratio = OUTPUT_SIZE / max(w, h)
    new_w, new_h = round(w * scale_ratio), round(h * scale_ratio)
    img_resized = img.resize((new_w, new_h))          # 先縮到顯示尺寸（RGB）
    img_resized = _remove_bg(img_resized)             # 再去背 → RGBA 人像貼圖（或退回不透明 RGBA）
    canvas = Image.new("RGBA", (OUTPUT_SIZE, OUTPUT_SIZE), (0, 0, 0, 0))
    offset_x = (OUTPUT_SIZE - new_w) // 2
    offset_y = (OUTPUT_SIZE - new_h) // 2
    # 直接複製 RGBA（不帶 mask）：把 rembg 算出的 alpha 原封不動搬進畫布，
    # 半透明髮絲邊緣保留原始 RGB，交給前端跟背景合成，不會產生黑邊。
    canvas.paste(img_resized, (offset_x, offset_y))
    return canvas


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
                    duration=frame_ms, loop=0, quality=85, exact=True)
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
            frame.save(fbuf, format="WEBP", quality=80, exact=True)
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
    actions = ["idle", "idle_tilt", "idle_glance", "idle_glance2", "idle_nod",
               "yawn", "alert", "talk_neutral", "talk_happy", "talk_sad", "talk_angry"]
    for idx, a in enumerate(actions, 1):
        log(f"  [{idx}/{len(actions)}] 烤製動作：{a} …")
        clips[a] = _bake_clip(pipeline, photo_path, src_eye_ratio, src_lip_ratio, a, scale)

    log(f"  烤製臉部同步角度網格 {GRID_DIM}×{GRID_DIM}（{GRID_DIM * GRID_DIM} 張,較耗時)…")
    grid = _bake_grid(pipeline, photo_path, src_eye_ratio, src_lip_ratio, scale)

    # 去背耗時統計：供評估「rembg 拖慢烤製的程度是否可接受」
    if REMOVE_BACKGROUND and _REMBG_STATS["frames"] > 0:
        n = _REMBG_STATS["frames"]
        total = _REMBG_STATS["seconds"]
        log(f"  rembg 去背統計：{n} 幀，共 {total:.1f}s，平均 {total / n * 1000:.0f} ms/幀"
            f"（此為單張 avatar 因去背而增加的總時間）")
    elif REMOVE_BACKGROUND and _REMBG_DISABLED:
        log("  rembg 去背未生效（已退回『不去背』）：這張 avatar 的背景沒有被挖掉，"
            "請確認 worker venv 是否已安裝 rembg。")

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
    parser.add_argument("--no-rembg", action="store_true",
                         help="關閉 rembg 人像去背（烤出含原背景的版本，方便跟去背版做 A/B 比較）")
    parser.add_argument("--rembg-model", default=None,
                         help=f"覆蓋去背模型名稱（預設 {REMBG_MODEL}；可試 isnet-general-use）")
    args = parser.parse_args()

    # CLI 旗標覆蓋模組層預設（worker 不走 argparse，仍吃上面的模組預設值）
    if args.no_rembg:
        REMOVE_BACKGROUND = False
    if args.rembg_model:
        REMBG_MODEL = args.rembg_model

    name = args.name or os.path.splitext(os.path.basename(args.photo))[0]
    bake(args.photo, name, args.lp_root, args.scale)
