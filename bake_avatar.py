#!/usr/bin/env python3
# ============================================================
#  bake_avatar.py — 本地 THA3 預烤腳本（雲端自訂頭像用）
#
#  用途：在你「本地」電腦（有 THA3 + torch + data/models 權重）把一張照片
#        烤成一個可攜的 .json 成品檔，裡面包含所有動畫（idle / 說話 / 打哈欠 /
#        叫醒）與「模式二臉部同步」用的角度網格，全部是 base64 webp。
#        把這個 .json 放進 baked_avatars/ 一起上傳到雲端，雲端就能「直接播」，
#        完全不需要在雲端跑神經網路。
#
#  使用方式（在你本地專案根目錄、且該資料夾有 tha3/ 與 data/models/）：
#       python bake_avatar.py 我的照片.jpg 貝貝
#  產出：baked_avatars/貝貝.json
#
#  之後：把 baked_avatars/貝貝.json commit 進 repo、推上 GitHub → 雲端模式二
#       的「自訂頭像」下拉就會出現「貝貝」可選。
#
#  注意：照片建議用「正面、清晰、單人、anime 風格」效果最好（THA3 對真實
#       照片較不自然，這是模型先天限制）。
# ============================================================
import io
import os
import sys
import json
import math
import base64
import hashlib

# ---- 這幾個是 THA3 必要的重量級依賴，只有本地才裝得起 ----
try:
    import numpy as np
    import torch
    import torchvision.transforms.functional as TF
    from PIL import Image
except Exception as e:
    print("✗ 缺少 THA3 需要的套件（torch / torchvision / pillow / numpy）。")
    print("  這個腳本必須在你本地、裝好 THA3 環境的地方執行。")
    print("  原始錯誤：", e)
    sys.exit(1)


# ============================================================
#  以下動畫參數與你 ui_pages.py 中的烤圖邏輯「完全一致」，
#  確保烤出來的成品和本地 app 一模一樣。
#  （若你日後改了 _build_pose，請把 ANIM_VERSION +1 並重新烤。）
# ============================================================
ANIM_VERSION = "v3"

ACTION_FRAMES = {
    "idle":         (50, 110),
    "yawn":         (28, 110),
    "alert":        (40, 120),
    "talk_neutral": (24, 90),
    "talk_happy":   (24, 90),
    "talk_sad":     (24, 90),
    "talk_angry":   (24, 90),
}
VISEMES = [26, 27, 29, 30, 28]

GRID_DIM = 7
GRID_HEAD_X_RANGE = 0.5
GRID_HEAD_Y_RANGE = 0.4


def _apply_expression(p, expr):
    if expr == "happy":
        p[8] = p[9] = 0.6
        p[34] = max(p[34], 0.45)
        p[35] = max(p[35], 0.45)
        p[18] = p[19] = 0.3
    elif expr == "sad":
        p[0] = p[1] = 0.6
        p[32] = p[33] = 0.4
        p[22] = p[23] = 0.2
        p[40] += 0.12
    elif expr == "angry":
        p[2] = p[3] = 0.7
        p[20] = p[21] = 0.35
        p[31] = 0.2


def _build_pose(action, t, i, n):
    p = [0.0] * 45
    if action == "idle":
        b = (math.sin(2*math.pi*t) + 0.30*math.sin(2*math.pi*2*t) + 0.12*math.sin(2*math.pi*3*t)) / 1.42
        p[44] = 0.45 + 0.40 * (b*0.5 + 0.5)
        p[42] = 0.012 * math.sin(2*math.pi*t)
        p[40] = 0.02 * math.sin(2*math.pi*t + 1.0)
        center = int(n * 0.82)
        d = abs(i - center)
        if d <= 2:
            p[12] = p[13] = max(0.0, 1.0 - d/3.0) ** 1.5
    elif action == "yawn":
        c = math.sin(math.pi * t)
        p[26] = c * 1.0
        p[12] = p[13] = c * 0.7
        p[6] = p[7] = c * 0.4
        p[40] = -0.12 * c
        p[44] = 0.5 + 0.4 * c
    elif action == "alert":
        p[40] = math.sin(t * 6 * math.pi) * 0.4
        p[16] = p[17] = 0.8
        p[6] = p[7] = 0.7
        p[26] = 0.5
        p[44] = 0.9
    elif action.startswith("talk"):
        p[44] = 0.45 + 0.25 * (math.sin(2*math.pi*t)*0.5 + 0.5)
        v = VISEMES[(i // 2) % len(VISEMES)]
        p[v] = 0.35 + 0.35 * abs(math.sin(i * 0.9))
        p[40] = 0.05 * math.sin(2*math.pi*2*t)
        p[6] = p[7] = 0.15 * abs(math.sin(2*math.pi*t))
        if action == "talk_happy":
            _apply_expression(p, "happy")
        elif action == "talk_sad":
            _apply_expression(p, "sad")
        elif action == "talk_angry":
            _apply_expression(p, "angry")
    return p


def _load_poser():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  → 載入 THA3 模型中（device={device}）…")
    from tha3.poser.modes.standard_float import create_poser
    poser = create_poser(device)
    return poser, device


def _bake_clip(poser, device, input_tensor, action):
    num_frames, frame_ms = ACTION_FRAMES.get(action, (40, 120))
    frames = []
    for i in range(num_frames):
        t = i / num_frames
        pose_params = _build_pose(action, t, i, num_frames)
        p_tensor = torch.tensor(pose_params, dtype=torch.float32).to(device).unsqueeze(0)
        with torch.no_grad():
            out_tensor = poser.pose(input_tensor, p_tensor)[0]
        rgb = (out_tensor[:3] + 1.0) / 2.0
        alpha = out_tensor[3:]
        out = torch.cat([rgb, alpha], dim=0).clamp(0, 1)
        out_np = (out.permute(1, 2, 0).cpu().numpy() * 255).astype(np.uint8)
        frames.append(Image.fromarray(out_np, "RGBA"))
    buf = io.BytesIO()
    frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:],
                   duration=frame_ms, loop=0, quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _bake_grid(poser, device, input_tensor):
    N = GRID_DIM
    xs = [(-GRID_HEAD_X_RANGE + 2 * GRID_HEAD_X_RANGE * (c / (N - 1))) for c in range(N)]
    ys = [(-GRID_HEAD_Y_RANGE + 2 * GRID_HEAD_Y_RANGE * (r / (N - 1))) for r in range(N)]
    frames_b64 = []
    for hy in ys:                 # row-major：外層列(y)、內層行(x)
        for hx in xs:
            pose_params = [0.0] * 45
            pose_params[39] = hx
            pose_params[40] = hy
            pose_params[42] = hx * 0.6
            pose_params[44] = 0.5
            p_tensor = torch.tensor(pose_params, dtype=torch.float32).to(device).unsqueeze(0)
            with torch.no_grad():
                out_tensor = poser.pose(input_tensor, p_tensor)[0]
            rgb = (out_tensor[:3] + 1.0) / 2.0
            alpha = out_tensor[3:]
            out = torch.cat([rgb, alpha], dim=0).clamp(0, 1)
            out_np = (out.permute(1, 2, 0).cpu().numpy() * 255).astype(np.uint8)
            frame = Image.fromarray(out_np, "RGBA")
            buf = io.BytesIO()
            frame.save(buf, format="WEBP", quality=80)
            frames_b64.append(base64.b64encode(buf.getvalue()).decode("utf-8"))
    return frames_b64


def bake(photo_path: str, out_name: str):
    if not os.path.exists(photo_path):
        print(f"✗ 找不到照片：{photo_path}")
        sys.exit(1)

    os.makedirs("baked_avatars", exist_ok=True)
    out_path = os.path.join("baked_avatars", f"{out_name}.json")

    print(f"開始烤製「{out_name}」← {photo_path}")
    pil_image = Image.open(photo_path).resize((512, 512)).convert("RGBA")

    poser, device = _load_poser()
    input_tensor = (TF.to_tensor(pil_image).to(device) * 2.0 - 1.0).unsqueeze(0)

    clips = {}
    actions = ["idle", "yawn", "alert", "talk_neutral", "talk_happy", "talk_sad", "talk_angry"]
    for idx, a in enumerate(actions, 1):
        print(f"  [{idx}/{len(actions)}] 烤製動作：{a} …")
        clips[a] = _bake_clip(poser, device, input_tensor, a)

    print(f"  烤製臉部同步角度網格 {GRID_DIM}×{GRID_DIM}（{GRID_DIM*GRID_DIM} 張）…")
    grid = _bake_grid(poser, device, input_tensor)

    payload = {
        "version": ANIM_VERSION,
        "gridDim": GRID_DIM,
        "clips": clips,        # idle / yawn / alert / talk_*（雲端依情緒選說話版）
        "grid": grid,          # 模式二臉部同步用
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"✓ 完成！已存成 {out_path}（{size_mb:.1f} MB）")
    print("  下一步：把這個檔 commit 進 repo、推上 GitHub，雲端模式二就會出現這個頭像。")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python bake_avatar.py <照片路徑> [輸出名稱]")
        print("範例：python bake_avatar.py my_photo.jpg 貝貝")
        sys.exit(1)
    photo = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) >= 3 else os.path.splitext(os.path.basename(photo))[0]
    bake(photo, name)
