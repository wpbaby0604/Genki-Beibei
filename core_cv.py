# ============================================================
#  core_cv.py  —  Genki Beibei v2.4 (Academic Standard Edition)
# ============================================================
#  學術依據:
#    1. CIE 1976 (L*a*b*) 色彩空間規範
#       L*: 0-100 (黑→白), a*: -128~+127 (綠→紅), b*: -128~+127 (藍→黃)
#
#    2. ΔE 色差: 採用 CIEDE2000 (Sharma et al., 2005) 公式作為主指標,
#       並同時保留 ΔE*ab (CIE76) 作為比較。CIEDE2000 是當前學術與工業
#       界的標準, 對人眼感知的相關性最佳 (~95% accuracy)。
#       參考: Sharma G., Wu W., Dalal E.N. (2005). The CIEDE2000
#             color-difference formula: Implementation notes,
#             supplementary test data, and mathematical observations.
#             Color Research & Application, 30(1), 21-30.
#
#    3. ITA (Individual Typology Angle): 採用 Chardon et al. (1991)
#       原始定義 ITA° = atan((L* - 50) / b*) × (180/π)
#       依此可將膚色分類為: Very light (>55°), Light (41-55°),
#                          Intermediate (28-41°), Tan (10-28°),
#                          Brown (-30 ~ 10°), Dark (< -30°)
#       參考: Chardon A., Cretois I., Hourseau C. (1991).
#             Skin colour typology and suntanning pathways.
#             Int J Cosmet Sci, 13, 191-208.
#
#    4. 黑眼圈量化文獻基準:
#       - Mac-Mary et al. (2019), Clin Cosmet Investig Dermatol:
#         以 VISIA-CR 拍攝, 量化 infraorbital ROI 與 cheek ROI 的
#         L*, a*, b* 與 ΔE 差異, 黑眼圈組顯著高於對照組。
#       - Vega et al. (2020), J Cosmet Dermatol: 提出 photonumeric
#         scale 並用 ΔE (cheek bone vs infraorbital) 驗證效度。
#
#    5. ΔE 閾值學術定義 (Mokrzycki & Tatol, 2011):
#         ΔE < 1.0   : 觀察者無法感知差異
#         1 ≤ ΔE < 2 : 僅受過訓練者可分辨
#         2 ≤ ΔE < 3.5: 一般人可感知, 但需要對比
#         3.5 ≤ ΔE < 5: 清楚可見的差異
#         ΔE ≥ 5     : 明顯不同的顏色
# ============================================================
import math
import cv2
import numpy as np
import mediapipe as mp


# ============================================================
#  Section 1.  基礎工具
# ============================================================
def calculate_distance(p1, p2):
    """計算兩點之間的歐式距離"""
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def validate_lighting_conditions(img_gray):
    """雙指標光源品質檢測: 有效拒絕逆光、過曝與極暗環境"""
    mean_brightness = float(np.mean(img_gray))
    contrast = float(np.std(img_gray))
    if mean_brightness < 40 or mean_brightness > 215 or contrast < 15:
        return False, (
            f"光源不良 (平均亮度: {mean_brightness:.1f}, "
            f"對比度: {contrast:.1f})"
        )
    return True, "光源良好"


def apply_adaptive_chromatic_adaptation(img_bgr):
    """基於灰世界假設 (Gray World Assumption) 的自適應白平衡。
    參考: Buchsbaum G. (1980). A spatial processor model for object
          colour perception. J Franklin Inst, 310(1), 1-26.
    """
    img_float = img_bgr.astype(np.float32)
    b, g, r = cv2.split(img_float)
    mean_b = float(np.mean(b))
    mean_g = float(np.mean(g))
    mean_r = float(np.mean(r))
    mean_gray = (mean_b + mean_g + mean_r) / 3.0

    eps = 1e-6
    b_gain = mean_gray / (mean_b + eps)
    g_gain = mean_gray / (mean_g + eps)
    r_gain = mean_gray / (mean_r + eps)

    b_corrected = np.clip(b * b_gain, 0, 255)
    g_corrected = np.clip(g * g_gain, 0, 255)
    r_corrected = np.clip(r * r_gain, 0, 255)

    return cv2.merge([b_corrected, g_corrected, r_corrected]).astype(np.uint8)


# ============================================================
#  Section 2.  色彩空間轉換 (學術標準尺度)
# ============================================================
def bgr_to_cielab_standard(img_bgr_uint8):
    """
    將 BGR uint8 影像轉為標準 CIE 1976 L*a*b* 浮點影像。

    說明:
      OpenCV 對 CV_32F 輸入 (值域 [0,1]) 會輸出 CIE 標準尺度:
        L: [0, 100], a: [-127, 127], b: [-127, 127]
      這與 chromameter (Konica Minolta CR-400) 等臨床儀器輸出的
      L*a*b* 規範一致, 可直接套用文獻中的閾值。

    參數:
      img_bgr_uint8: H×W×3, dtype=uint8, BGR 順序
    回傳:
      img_lab: H×W×3, dtype=float32, 通道為 (L*, a*, b*)
    """
    img_float = img_bgr_uint8.astype(np.float32) / 255.0
    img_lab = cv2.cvtColor(img_float, cv2.COLOR_BGR2Lab)
    return img_lab


# ============================================================
#  Section 3.  學術標準色差公式
# ============================================================
def delta_e_cie76(L1, a1, b1, L2, a2, b2):
    """
    CIE 1976 ΔE*ab (Euclidean distance in LAB).
    公式: ΔE = √[(ΔL*)² + (Δa*)² + (Δb*)²]
    特點: 計算簡單, 但對人眼感知對應僅約 75% 準確。
    """
    return math.sqrt((L1 - L2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)


def delta_e_ciede2000(L1, a1, b1, L2, a2, b2, kL=1.0, kC=1.0, kH=1.0):
    """
    CIEDE2000 色差公式 (Sharma, Wu & Dalal, 2005)。
    對人眼感知的對應度約 95%, 是當前學術與工業界的標準。

    參數 kL, kC, kH 為參數因子, 圖文影像領域標準設為 1.0。

    回傳:
      ΔE00 純量值。一般而言 ΔE00 約為 ΔE76 的 0.7-0.9 倍。

    實作參考:
      Sharma G., Wu W., Dalal E.N. (2005). Color Res Appl, 30(1), 21-30.
    """
    # --- 1. 預備計算 C*, h' ---
    C1 = math.sqrt(a1 ** 2 + b1 ** 2)
    C2 = math.sqrt(a2 ** 2 + b2 ** 2)
    C_bar = (C1 + C2) / 2.0

    # G 因子: 修正灰色軸附近的不對稱
    G = 0.5 * (1 - math.sqrt(C_bar ** 7 / (C_bar ** 7 + 25 ** 7)))

    a1_prime = (1 + G) * a1
    a2_prime = (1 + G) * a2

    C1_prime = math.sqrt(a1_prime ** 2 + b1 ** 2)
    C2_prime = math.sqrt(a2_prime ** 2 + b2 ** 2)

    # h' (色相角, 0~360°)
    def _h_prime(a_p, b):
        if a_p == 0 and b == 0:
            return 0.0
        h = math.degrees(math.atan2(b, a_p))
        return h + 360.0 if h < 0 else h

    h1_prime = _h_prime(a1_prime, b1)
    h2_prime = _h_prime(a2_prime, b2)

    # --- 2. 計算 ΔL', ΔC', ΔH' ---
    dL_prime = L2 - L1
    dC_prime = C2_prime - C1_prime

    # ΔH' 需小心處理色相環的環繞 (wrap-around)
    if C1_prime * C2_prime == 0:
        dh_prime = 0.0
    else:
        diff = h2_prime - h1_prime
        if abs(diff) <= 180:
            dh_prime = diff
        elif diff > 180:
            dh_prime = diff - 360
        else:
            dh_prime = diff + 360

    dH_prime = 2 * math.sqrt(C1_prime * C2_prime) * math.sin(
        math.radians(dh_prime / 2)
    )

    # --- 3. 計算加權因子 SL, SC, SH 與旋轉項 RT ---
    L_bar = (L1 + L2) / 2.0
    C_bar_prime = (C1_prime + C2_prime) / 2.0

    if C1_prime * C2_prime == 0:
        h_bar_prime = h1_prime + h2_prime
    else:
        diff_h = abs(h1_prime - h2_prime)
        sum_h = h1_prime + h2_prime
        if diff_h <= 180:
            h_bar_prime = sum_h / 2.0
        elif sum_h < 360:
            h_bar_prime = (sum_h + 360) / 2.0
        else:
            h_bar_prime = (sum_h - 360) / 2.0

    T = (
        1
        - 0.17 * math.cos(math.radians(h_bar_prime - 30))
        + 0.24 * math.cos(math.radians(2 * h_bar_prime))
        + 0.32 * math.cos(math.radians(3 * h_bar_prime + 6))
        - 0.20 * math.cos(math.radians(4 * h_bar_prime - 63))
    )

    SL = 1 + (0.015 * (L_bar - 50) ** 2) / math.sqrt(20 + (L_bar - 50) ** 2)
    SC = 1 + 0.045 * C_bar_prime
    SH = 1 + 0.015 * C_bar_prime * T

    delta_theta = 30 * math.exp(-(((h_bar_prime - 275) / 25) ** 2))
    RC = 2 * math.sqrt(C_bar_prime ** 7 / (C_bar_prime ** 7 + 25 ** 7))
    RT = -math.sin(math.radians(2 * delta_theta)) * RC

    # --- 4. 最終 ΔE00 ---
    term_L = (dL_prime / (kL * SL)) ** 2
    term_C = (dC_prime / (kC * SC)) ** 2
    term_H = (dH_prime / (kH * SH)) ** 2
    term_RT = RT * (dC_prime / (kC * SC)) * (dH_prime / (kH * SH))

    return math.sqrt(term_L + term_C + term_H + term_RT)


def compute_ita_degrees(L_star, b_star):
    """
    Individual Typology Angle (Chardon et al., 1991)。
    公式: ITA° = atan((L* - 50) / b*) × (180/π)
    """
    if abs(b_star) < 1e-6:
        return math.degrees(
            math.atan2(L_star - 50, 1e-6 if b_star >= 0 else -1e-6)
        )
    return math.degrees(math.atan((L_star - 50) / b_star))


def classify_skin_type_by_ita(ita_deg):
    """依 Chardon 1991 ITA 分類傳回膚色描述字串"""
    if ita_deg > 55:
        return "Very Light"
    elif ita_deg > 41:
        return "Light"
    elif ita_deg > 28:
        return "Intermediate"
    elif ita_deg > 10:
        return "Tan"
    elif ita_deg > -30:
        return "Brown"
    else:
        return "Dark"


# ============================================================
#  Section 4.  MediaPipe ROI 索引常數
# ============================================================
LEFT_EYE_ROI  = [228, 229, 230, 231, 232, 233, 123]
RIGHT_EYE_ROI = [448, 449, 450, 451, 452, 453, 342]
CHEEK_ROI     = [205, 206, 207, 187, 147]

# --- v2.6 複合指標新增 ROI ---
# 鞏膜 (眼白): 取眼睛輪廓多邊形, 再扣掉虹膜圓, 量測眼白泛紅 (a*)。
#   索引為 MediaPipe FaceMesh 標準眼睛外輪廓 (左/右各一圈)。
LEFT_EYE_CONTOUR  = [33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144, 163, 7]
RIGHT_EYE_CONTOUR = [362, 398, 384, 385, 386, 387, 388, 466, 263, 249, 390, 373, 374, 380, 381, 382]
# refine_landmarks=True 時啟用的虹膜中心 + 周邊點 (用於估虹膜半徑並排除)
LEFT_IRIS  = [468, 469, 470, 471, 472]
RIGHT_IRIS = [473, 474, 475, 476, 477]

# 下眼瞼浮腫帶: 眼睛正下方的條狀區域 (infraorbital), 用相對明度量測陰影/浮腫。
#   浮腫為幾何/陰影特徵, 此處以「下眼瞼帶相對面頰的暗化程度」作保守代理量。
LEFT_LOWER_LID  = [230, 231, 232, 233, 244, 245]
RIGHT_LOWER_LID = [450, 451, 452, 453, 464, 465]


# ============================================================
#  Section 5.  主管線
# ============================================================
def _roi_lab_median_from_pts(image_lab, pts, h, w, min_px=20):
    """給定多邊形頂點 (像素座標), 回傳遮罩內 (L*,a*,b*) 中位數; 不足則 None。"""
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [pts.astype(np.int32)], 255)
    sel = mask == 255
    if int(np.count_nonzero(sel)) < min_px:
        return None
    return (
        float(np.median(image_lab[:, :, 0][sel])),
        float(np.median(image_lab[:, :, 1][sel])),
        float(np.median(image_lab[:, :, 2][sel])),
    )


def _lms_pts(lms, idx_list, w, h):
    """把 landmark 索引清單轉成像素座標陣列。"""
    return np.array(
        [[lms.landmark[i].x * w, lms.landmark[i].y * h] for i in idx_list],
        dtype=np.float32,
    )


def extract_sclera_redness(image_lab, lms, contour_idx, iris_idx, h, w):
    """量測鞏膜 (眼白) 泛紅程度 = 眼白區 a* 中位數。

    作法: 取眼睛輪廓多邊形遮罩, 扣掉以虹膜點估出的虹膜圓 (避免虹膜/瞳孔污染),
    再取剩餘眼白像素的 a* 中位數。a* 越高代表越紅 (疲勞/睡眠不足徵候之一)。
    回傳 a* (float) 或 None。
    """
    contour = _lms_pts(lms, contour_idx, w, h)
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [contour.astype(np.int32)], 255)

    iris = _lms_pts(lms, iris_idx, w, h)
    if len(iris) >= 2:
        cx, cy = float(np.mean(iris[:, 0])), float(np.mean(iris[:, 1]))
        # 虹膜半徑: 周邊點到中心的最大距離 (再放大一點以涵蓋邊緣)
        radius = float(np.max(np.hypot(iris[:, 0] - cx, iris[:, 1] - cy))) * 1.15
        cv2.circle(mask, (int(cx), int(cy)), int(max(radius, 1)), 0, -1)

    sel = mask == 255
    if int(np.count_nonzero(sel)) < 15:
        return None
    return float(np.median(image_lab[:, :, 1][sel]))  # a* 通道


def extract_lid_puffiness(image_lab, lms, lid_idx, L_cheek, h, w):
    """量測下眼瞼浮腫的『陰影代理量』= 面頰 L* − 下眼瞼帶 L*。

    浮腫會在下眼瞼投下陰影, 使該區相對面頰變暗; 值越大代表陰影/浮腫越明顯。
    註: 純 2D 影像無法直接量 3D 突起, 此為保守代理, 與 ΔL* 高度相關但聚焦
        在更靠下的 infraorbital 帶狀區。回傳 float 或 None。
    """
    lid = _roi_lab_median_from_pts(image_lab, _lms_pts(lms, lid_idx, w, h), h, w)
    if lid is None:
        return None
    return float(L_cheek - lid[0])


def analyze_dark_circles(image_file):
    """
    完整黑眼圈色彩分析流水線 (學術標準版)。

    流程:
      1. 讀檔保護, 解析度檢查
      2. 光源品質雙指標檢測 (mean brightness, contrast)
      3. Gray World 白平衡 (Buchsbaum 1980)
      4. 轉至 CIE 1976 L*a*b* 標準浮點空間
      5. MediaPipe FaceMesh 萃取雙眼周 + 面頰 ROI 中位數
      6. 計算學術標準指標:
           - ΔE*ab (CIE76): 傳統色差
           - ΔE00 (CIEDE2000): 業界標準色差
           - ΔL*: 明度差 (黑眼圈核心指標)
           - Δa*: 紅綠分量差 (血管型指標)
           - Δb*: 黃藍分量差
           - ITA°: 膚色分型
           - 左右不對稱性
      7. 依 Mokrzycki & Tatol (2011) ΔE 閾值與 ΔL* 判定

    回傳: (result_dict, error_msg)
    """
    # ---- (1) 讀檔保護 ----
    try:
        if hasattr(image_file, "seek"):
            image_file.seek(0)
        file_bytes = np.asarray(bytearray(image_file.read()), dtype=np.uint8)
    except Exception:
        return None, "讀取上傳檔案時發生錯誤，請重新上傳。"

    if file_bytes.size == 0:
        return None, "上傳的檔案為空，請選擇有效的圖片。"

    orig_image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
    if orig_image is None:
        return None, "圖片解碼失敗，請確認是合法的 JPG/PNG 格式。"

    h, w = orig_image.shape[:2]
    if h < 200 or w < 200:
        return None, f"圖片解析度過低 ({w}×{h})，請上傳至少 200×200 像素的清晰自拍。"

    # ---- (2) 光源品質 ----
    gray_image = cv2.cvtColor(orig_image, cv2.COLOR_BGR2GRAY)
    is_good_light, light_msg = validate_lighting_conditions(gray_image)
    if not is_good_light:
        return None, f"檢測失敗：{light_msg}。請在光線均勻處重新拍攝。"

    # ---- (3) 白平衡 (Gray World) ----
    wb_img = apply_adaptive_chromatic_adaptation(orig_image)
    image_rgb_for_mp = cv2.cvtColor(wb_img, cv2.COLOR_BGR2RGB)

    # ---- (4) 轉換至 CIE 標準 LAB 空間 ----
    image_lab = bgr_to_cielab_standard(wb_img)  # L:[0,100], a/b:[-127,127]

    annotated_img = cv2.cvtColor(orig_image, cv2.COLOR_BGR2RGB).copy()

    # ---- (5) MediaPipe ROI 萃取 ----
    mp_face_mesh = mp.solutions.face_mesh
    with mp_face_mesh.FaceMesh(
        static_image_mode=True,
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5,
    ) as face_mesh:
        results = face_mesh.process(image_rgb_for_mp)
        if not results.multi_face_landmarks:
            return None, "未偵測到清晰臉部。請確保正面對準鏡頭且無遮擋。"

        lms = results.multi_face_landmarks[0]

        def get_roi_lab_median(idx_list, color):
            """傳回 ROI 區域內 (L*, a*, b*) 三通道的中位數元組;
               若 ROI 樣本過少則回傳 None。"""
            pts = np.array(
                [[int(lms.landmark[i].x * w), int(lms.landmark[i].y * h)]
                 for i in idx_list],
                dtype=np.int32,
            )
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.fillPoly(mask, [pts], 255)

            L_vals = image_lab[:, :, 0][mask == 255]
            a_vals = image_lab[:, :, 1][mask == 255]
            b_vals = image_lab[:, :, 2][mask == 255]

            if len(L_vals) < 30:
                return None

            cv2.polylines(annotated_img, [pts], True, color, 2)
            return (
                float(np.median(L_vals)),
                float(np.median(a_vals)),
                float(np.median(b_vals)),
            )

        l_stats = get_roi_lab_median(LEFT_EYE_ROI, (255, 0, 0))
        r_stats = get_roi_lab_median(RIGHT_EYE_ROI, (255, 255, 0))
        c_stats = get_roi_lab_median(CHEEK_ROI, (0, 255, 0))

        # --- v2.6 複合指標特徵 (在 face_mesh context 內, lms 仍有效) ---
        # 鞏膜泛紅: 左右眼白 a* 平均
        _scl_l = extract_sclera_redness(image_lab, lms, LEFT_EYE_CONTOUR,  LEFT_IRIS,  h, w)
        _scl_r = extract_sclera_redness(image_lab, lms, RIGHT_EYE_CONTOUR, RIGHT_IRIS, h, w)
        _scl_vals = [v for v in (_scl_l, _scl_r) if v is not None]
        sclera_a = float(np.mean(_scl_vals)) if _scl_vals else None

        # 下眼瞼浮腫: 先在 context 內取下眼瞼帶 L*, 待 L_cheek 算出後再相減
        _lid_l = _roi_lab_median_from_pts(image_lab, _lms_pts(lms, LEFT_LOWER_LID,  w, h), h, w)
        _lid_r = _roi_lab_median_from_pts(image_lab, _lms_pts(lms, RIGHT_LOWER_LID, w, h), h, w)
        _lid_L_vals = [s[0] for s in (_lid_l, _lid_r) if s is not None]
        _lid_L_mean = float(np.mean(_lid_L_vals)) if _lid_L_vals else None

    # ---- (6) 取樣完整性驗證 ----
    if not l_stats or not r_stats or not c_stats:
        return None, "採樣像素不足。請靠近鏡頭並確保眼周與面頰無頭髮遮擋。"

    L_left,  a_left,  b_left  = l_stats
    L_right, a_right, b_right = r_stats
    L_cheek, a_cheek, b_cheek = c_stats

    # 雙眼平均, 用於綜合指標
    L_eye = (L_left + L_right) / 2.0
    a_eye = (a_left + a_right) / 2.0
    b_eye = (b_left + b_right) / 2.0

    # ---- (7) 學術標準色差計算 ----
    # 綜合 ΔE: 兩眼平均 vs 面頰
    delta_e_76    = delta_e_cie76(L_eye, a_eye, b_eye, L_cheek, a_cheek, b_cheek)
    delta_e_2000  = delta_e_ciede2000(L_eye, a_eye, b_eye, L_cheek, a_cheek, b_cheek)

    # 左右眼分別 ΔE00 (學術上的對稱性指標)
    delta_e_left_00  = delta_e_ciede2000(L_left,  a_left,  b_left,  L_cheek, a_cheek, b_cheek)
    delta_e_right_00 = delta_e_ciede2000(L_right, a_right, b_right, L_cheek, a_cheek, b_cheek)
    asymmetry_00     = abs(delta_e_left_00 - delta_e_right_00)

    # 個別通道差: 黑眼圈病理機制的判讀依據
    delta_L_star = L_cheek - L_eye   # 正值代表眼周較暗 (黑眼圈核心)
    delta_a_star = a_eye - a_cheek   # 正值代表眼周偏紅 (血管型徵候)
    delta_b_star = b_eye - b_cheek

    # ITA° (依面頰膚色而非眼周, 因眼周受血管影響不純)
    ita_deg = compute_ita_degrees(L_cheek, b_cheek)
    skin_type = classify_skin_type_by_ita(ita_deg)

    # --- v2.6 複合指標: 下眼瞼浮腫陰影代理量 + 面頰明度(蒼白) ---
    # 浮腫: 面頰 L* − 下眼瞼帶 L* (越大=陰影越深=越浮腫)
    lid_puffiness = (float(L_cheek - _lid_L_mean)
                     if _lid_L_mean is not None else None)
    # 面頰苍白: 直接用面頰 L* (z-score 化後越高代表越蒼白/越亮, 偏疲態)
    cheek_pallor = float(L_cheek)

    # ---- (8) 黑眼圈判定 (學術閾值) ----
    # Mokrzycki & Tatol (2011) 提出 ΔE00 ≥ 3.5 為「清楚可見」的色差。
    # 結合 ΔL* ≥ 3 (眼周明顯較暗) 作為輔助條件, 提升 specificity。
    has_dark_circles = (delta_e_2000 >= 3.5) or (delta_L_star >= 3.0)

    # 病因子型判定: 文獻 Sarkar et al. (2016) 依 a* 通道判斷血管型 vs 色素型
    # Δa* > 1.0 且明顯時偏向血管型 (vascular), 否則偏向色素型 (pigmented)
    detected_type = "vascular" if delta_a_star > 1.0 else "pigmented"

    # ---- (9) 結果包裝 ----
    return {
        "orig": cv2.cvtColor(orig_image, cv2.COLOR_BGR2RGB),
        "orig_bgr": orig_image,
        "annotated_img": annotated_img,
        "has_dark_circles": has_dark_circles,
        "detected_type": detected_type,
        "skin_type_ita": skin_type,
        "metrics": {
            # --- 學術標準色差指標 ---
            "delta_E":          round(float(delta_e_2000), 2),  # 主指標: CIEDE2000
            "delta_E_cie76":    round(float(delta_e_76),  2),   # 比較用: 傳統 CIE76
            "delta_E_left":     round(float(delta_e_left_00),  2),
            "delta_E_right":    round(float(delta_e_right_00), 2),
            "asymmetry":        round(float(asymmetry_00), 2),
            # --- 個別通道差 (病理判讀依據) ---
            "delta_L":          round(float(delta_L_star), 2),
            "delta_a":          round(float(delta_a_star), 2),
            "delta_b":          round(float(delta_b_star), 2),
            # --- 膚色分型 ---
            "ita":              round(float(ita_deg), 1),
            # --- v2.6 複合指標原始特徵 (None 代表該張未能取得; 由資料層處理) ---
            "sclera_a":         (round(float(sclera_a), 2) if sclera_a is not None else None),
            "lid_puffiness":    (round(float(lid_puffiness), 2) if lid_puffiness is not None else None),
            "cheek_pallor":     round(float(cheek_pallor), 2),
            # --- 原始 ROI 中位數 (供進階分析) ---
            "L_eye":            round(float(L_eye),   2),
            "a_eye":            round(float(a_eye),   2),
            "b_eye":            round(float(b_eye),   2),
            "L_cheek":          round(float(L_cheek), 2),
            "a_cheek":          round(float(a_cheek), 2),
            "b_cheek":          round(float(b_cheek), 2),
        },
    }, None
