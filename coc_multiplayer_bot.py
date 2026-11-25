#!/usr/bin/env python3
"""
Clash of Clans (CoC) 家鄉多人聯機戰鬥 (Home Village Multiplayer) 通用全自動打資源/打星腳本
沿用 coc_night_witch_bot.py 架構，並針對 OPPO CPH2723 (2640x1216) 實機校準。

核心功能：
1. 【完整家鄉進攻流程狀態機】：
   - 支援新版 CoC 多人對戰介面：家鄉主畫面「戰鬥」→「一般戰鬥：尋找對戰目標」→「我的軍隊：攻擊！」→ 偵察對手畫面。
   - 每個步驟皆具備視覺像素確認與自動重試（自癒點擊），並可選 `--mode ranked` 切到排位戰。
2. 【零外部依賴極速資源辨識 & 自動挑魚 (Nexting)】：
   - 內建針對 CoC 遊戲字體校準之 12x16 數字範本（不需安裝 Tesseract / EasyOCR，單次辨識 <15ms），
     精準讀取左上角可掠奪「金幣 / 聖水 / 闇黑重油」。
   - 支援 `--min-gold`、`--min-elixir`、`--min-dark`、`--min-total`（金+水總和）與 `--max-next` 上限；
     不達標自動點右下角「下一個」，也可加 `--no-filter` 直接開打。
3. 【通用動態兵種列掃描 (適用任意配兵)】：
   - 自動掃描底部兵種列每一張卡牌，不綁定固定兵種順序，自動分類：
     * 一般／活動兵種 (`troop`) + 讀取右上角 `xN` 數量
     * 攻城機器／部落城堡 (`siege`)
     * 所有英雄 (`hero`) — 自動避開守護者地面/空中切換鈕
     * 所有法術 (`spell`) + 讀取 `xN` 數量
4. 【紅線偵測一字排開下兵 + 自動補兵 + 分波法術 + 智慧英雄技能 + 秒回營】：
   - 選兵後自動掃描地圖紅色禁止部署線，計算紅線外側安全草地一字排開下兵（支援大量兵種自動展開／長按滑動）。
   - 逐格檢查漏放兵自動換點補放；兵種推進後自動朝陣型中心方向分波下法術；
     英雄進場後自動觸發存活英雄技能；結算「回營」一出現立刻點擊回村並關閉獎勵彈窗。
"""

import argparse
from dataclasses import dataclass
import io
import random
import re
import struct
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# =====================================================================
# 以 OPPO CPH2723 (2640 x 1216) 實機校準之基準比例座標
# =====================================================================
BASE_W, BASE_H = 2640, 1216

# 底部卡牌頭像正中央點擊高度（避開上方 xN 數字與下方大守護者空陸切換鈕 y=1145..1185）
CARD_TAP_Y = 1080

COORDS_RATIO = {
    # 1. 家鄉主畫面左下角黃色「戰鬥」(Attack) 按鈕 (x=154..364, y=978..1183)
    "attack_btn": (259 / BASE_W, 1080 / BASE_H),

    # 2. 多人遊戲彈窗：左側「一般戰鬥」橘色「尋找對戰目標」按鈕 (x=210..690, y=848..968)
    "normal_find_btn": (450 / BASE_W, 908 / BASE_H),

    #    右側「排位戰」橘色「尋找對戰目標」按鈕 (x=810..1290, y=848..968)
    "ranked_find_btn": (1050 / BASE_W, 908 / BASE_H),

    # 3. 「我的軍隊」確認視窗右下角綠色「攻擊！」按鈕 (x=1968..2376, y=1020..1136)
    "army_attack_btn": (2172 / BASE_W, 1078 / BASE_H),

    # 4. 偵察畫面右下角橘色「下一個」(Next) 按鈕 (x=2132..2492, y=750..918)
    "next_btn": (2312 / BASE_W, 834 / BASE_H),

    # 5. 偵察/戰鬥畫面左下角紅色「結束戰鬥」(End Battle) 按鈕 (x=152..392, y=872..940)
    "end_battle_btn": (272 / BASE_W, 906 / BASE_H),

    # 6. 投降確認視窗綠色「確定」(Okay) 按鈕
    "surrender_ok_btn": (1530 / BASE_W, 745 / BASE_H),

    # 7. 結算畫面正下方綠色「回營」(Return Home) 預設座標 (偵測到時會自動使用實際偵測 Y)
    "return_home_btn": (1320 / BASE_W, 1035 / BASE_H),

    # 8. 勝利之星獎勵 / 聯賽彈窗「確認」按鈕
    "star_bonus_ok_btn": (1320 / BASE_W, 968 / BASE_H),

    # 9. 備用外圍下兵線 (僅在非草地場景且紅線偵測皆無結果時使用，貼近最外側邊緣避開紅框)
    "deploy_line_left": [
        (560 / BASE_W, 170 / BASE_H),
        (530 / BASE_W, 250 / BASE_H),
        (210 / BASE_W, 430 / BASE_H),
        (140 / BASE_W, 520 / BASE_H),
        (240 / BASE_W, 650 / BASE_H),
        (360 / BASE_W, 725 / BASE_H),
        (480 / BASE_W, 795 / BASE_H),
    ],
    #    備用補兵點 (另一側外圍邊緣：右上 + 右下)
    "deploy_line_retry": [
        (1960 / BASE_W, 240 / BASE_H),
        (2100 / BASE_W, 330 / BASE_H),
        (2380 / BASE_W, 450 / BASE_H),
        (2470 / BASE_W, 530 / BASE_H),
        (2350 / BASE_W, 610 / BASE_H),
        (2180 / BASE_W, 685 / BASE_H),
        (105 / BASE_W, 760 / BASE_H),
    ],
    # 10. 法術投放點 (從左側推進至陣型核心沿線，涵蓋狂暴/治療/冰凍等)
    "spell_points": [
        (820 / BASE_W, 490 / BASE_H),
        (980 / BASE_W, 430 / BASE_H),
        (1060 / BASE_W, 560 / BASE_H),
        (1220 / BASE_W, 470 / BASE_H),
        (1140 / BASE_W, 360 / BASE_H),
        (1340 / BASE_W, 490 / BASE_H),
    ],
}


# =====================================================================
# CoC 遊戲數字 12x16 二值化範本 (於 2640x1216 實機可掠奪資源字體提取)
# =====================================================================
DIGIT_HEX_12X16: Dict[str, List[int]] = {
    "0": [0x010, 0x7FC, 0x7FE, 0xFFE, 0xF9F, 0xF1F, 0xF1F, 0xF1F, 0xF1F, 0xF1F, 0xF1F, 0xF1F, 0xF1F, 0xFFE, 0x7FE, 0x7FC],
    "1": [0x3FF, 0xFFF, 0xFFF, 0x3FF, 0x1FF, 0x1FF, 0x1FF, 0x1FF, 0x1FF, 0x1FF, 0x1FF, 0x1FE, 0x1FE, 0x1FE, 0x1FE, 0x1F0],
    "2": [0x3FE, 0x7FE, 0x7FF, 0x01F, 0x01F, 0x01F, 0x03F, 0x0FE, 0x3FC, 0x7F0, 0xFE0, 0xF80, 0xFC0, 0xFFF, 0x7FF, 0x7FE],
    "3": [0x7F8, 0x7FE, 0xFFE, 0x03E, 0x03E, 0x03C, 0x1F8, 0x1FC, 0x1FE, 0x01F, 0x01F, 0x03F, 0x1FF, 0xFFE, 0xFFE, 0xF00],
    "4": [0x0FC, 0x0FC, 0x1FC, 0x1FC, 0x3DC, 0x39C, 0x79C, 0x79C, 0x71C, 0xFFE, 0xFFF, 0x7FF, 0x73E, 0x01C, 0x01C, 0x01C],
    "5": [0x0FE, 0x7FE, 0x7FE, 0x7C0, 0x780, 0xF80, 0xFC0, 0xFFE, 0xFFF, 0x07F, 0x03F, 0x03F, 0x1FE, 0x7FE, 0x7F8, 0x700],
    "6": [0x00C, 0x7FE, 0x7FE, 0xFF0, 0xF80, 0xF00, 0xF80, 0xFFE, 0xFFF, 0xFFF, 0xF0F, 0xF0F, 0xF0F, 0xFFE, 0xFFE, 0x3FC],
    "7": [0x7FF, 0xFFF, 0xFFF, 0x03F, 0x03E, 0x07E, 0x07C, 0x0FC, 0x0F8, 0x1F0, 0x1F0, 0x3E0, 0x3E0, 0x7C0, 0x7C0, 0x780],
    "8": [0x3FC, 0x7FC, 0x7FE, 0xF3E, 0xF1E, 0xF1C, 0x7FC, 0x3F8, 0x7FC, 0xFFE, 0xF1F, 0xF1E, 0xF1E, 0xFFE, 0xFFE, 0x7FC],
    "9": [0x3FE, 0x7FE, 0x7FE, 0x7FE, 0x78F, 0xF8F, 0xFFF, 0xFFF, 0xFFF, 0x7FF, 0x00F, 0x00F, 0x07F, 0x3FF, 0x3FE, 0x3E0],
}


def _build_digit_templates() -> Dict[str, "np.ndarray"]:
    if not HAS_NUMPY:
        return {}
    tmpls = {}
    for ch, rows in DIGIT_HEX_12X16.items():
        mat = np.zeros((16, 12), dtype=bool)
        for r_idx, val in enumerate(rows):
            for c_idx in range(12):
                if (val >> (11 - c_idx)) & 1:
                    mat[r_idx, c_idx] = True
        tmpls[ch] = mat
    return tmpls


DIGIT_TEMPLATES = _build_digit_templates()


@dataclass
class CardSlot:
    index: int
    x1: int       # 基準座標 (2640 空間)
    x2: int
    cx: int
    kind: str     # "troop" | "siege" | "hero" | "spell"
    count: int    # 兵種/法術數量 (xN)，英雄/攻城機器為 1
    active: bool  # 頭像仍為彩色（尚未用盡／未陣亡）


# =====================================================================
# ADB 與快速截圖基礎函式
# =====================================================================

def run_adb(args: list, serial: Optional[str] = None) -> str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.stdout.strip()


def capture_screen_pil(serial: Optional[str] = None) -> Optional["Image.Image"]:
    """
    透過未壓縮 `adb exec-out screencap` 零拷貝快速轉為 PIL Image (~0.9s)。
    若格式不符則自動退回 `screencap -p`。
    """
    if not HAS_PIL:
        return None
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])

    try:
        res = subprocess.run(
            cmd + ["exec-out", "screencap"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=4,
        )
        raw = res.stdout
        if res.returncode == 0 and len(raw) > 16:
            w, h, _ = struct.unpack("<III", raw[:12])
            expected_pixels = w * h * 4
            if 0 < w <= 4096 and 0 < h <= 4096 and len(raw) >= expected_pixels + 12:
                hdr = len(raw) - expected_pixels
                return Image.frombuffer("RGBA", (w, h), raw[hdr:], "raw", "RGBA", 0, 1)
    except Exception:
        pass

    try:
        res = subprocess.run(
            cmd + ["exec-out", "screencap", "-p"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout:
            return Image.open(io.BytesIO(res.stdout)).convert("RGB")
    except Exception:
        pass
    return None


def get_screen_landscape_size(serial: Optional[str] = None) -> Tuple[int, int]:
    out = run_adb(["shell", "wm", "size"], serial)
    m = re.search(r"(\d+)x(\d+)", out)
    if not m:
        return BASE_W, BASE_H
    d1, d2 = int(m.group(1)), int(m.group(2))
    return max(d1, d2), min(d1, d2)


def _px(img: "Image.Image", base_x: float, base_y: float) -> Tuple[int, int, int]:
    w, h = img.size
    x = min(w - 1, max(0, int((base_x / BASE_W) * w)))
    y = min(h - 1, max(0, int((base_y / BASE_H) * h)))
    p = img.getpixel((x, y))
    return p[0], p[1], p[2]


def _to_base_rgb(img: "Image.Image") -> Optional["np.ndarray"]:
    """將截圖轉為 2640x1216 RGB numpy array（解析度相同時零縮放成本）。"""
    if not HAS_NUMPY or img is None:
        return None
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    if rgb.size != (BASE_W, BASE_H):
        rgb = rgb.resize((BASE_W, BASE_H), Image.BILINEAR)
    return np.asarray(rgb)


# =====================================================================
# 畫面狀態像素偵測
# =====================================================================

def is_home_screen_visible(img: Optional["Image.Image"]) -> bool:
    """檢查是否在家鄉主村莊畫面 (左下角亮黃色『戰鬥』按鈕未受彈窗遮暗)"""
    if img is None:
        return False
    r1, g1, b1 = _px(img, 259, 1000)
    r2, g2, b2 = _px(img, 180, 1150)
    return (r1 > 220 and g1 > 175 and b1 < 50) and (r2 > 220 and g2 > 165 and b2 < 50)


def is_attack_modal_visible(img: Optional["Image.Image"]) -> bool:
    """檢查是否已開啟『多人遊戲』進攻視窗 (左側『一般戰鬥』橘黃按鈕 + 左側棕色底板)"""
    if img is None:
        return False
    r1, g1, b1 = _px(img, 450, 908)
    r2, g2, b2 = _px(img, 260, 860)
    br, bg, bb = _px(img, 100, 300)
    is_orange1 = r1 > 225 and 145 <= g1 <= 220 and b1 < 80
    is_orange2 = r2 > 225 and 145 <= g2 <= 220 and b2 < 80
    is_brown = 100 <= br <= 175 and 60 <= bg <= 120 and 35 <= bb <= 95 and br > bg > bb
    return is_orange1 and is_orange2 and is_brown


def is_army_confirm_visible(img: Optional["Image.Image"]) -> bool:
    """檢查是否已開啟『我的軍隊』確認視窗 (右下角綠色『攻擊！』按鈕 + 上方棕色標題底板)"""
    if img is None:
        return False
    r1, g1, b1 = _px(img, 2050, 1078)
    r2, g2, b2 = _px(img, 2320, 1078)
    br, bg, bb = _px(img, 1400, 180)
    is_green1 = g1 > 180 and r1 > 90 and g1 > b1 + 60
    is_green2 = g2 > 180 and r2 > 90 and g2 > b2 + 60
    is_brown = 100 <= br <= 175 and 60 <= bg <= 120 and 35 <= bb <= 95 and br > bg > bb
    return is_green1 and is_green2 and is_brown


def is_end_battle_visible(img: Optional["Image.Image"]) -> bool:
    """檢查左下角紅色『結束戰鬥』按鈕是否可見 (偵察或戰鬥進行中皆存在)"""
    if img is None:
        return False
    r1, g1, b1 = _px(img, 200, 906)
    r2, g2, b2 = _px(img, 350, 906)
    is_red1 = r1 > 200 and g1 < 125 and b1 < 125 and r1 > g1 + 100
    is_red2 = r2 > 200 and g2 < 125 and b2 < 125 and r2 > g2 + 100
    return is_red1 and is_red2


def is_next_btn_visible(img: Optional["Image.Image"]) -> bool:
    """檢查右下角橘黃色『下一個』按鈕是否可見 (上緣亮橘黃 + 下緣深橘雙色校驗，開打後消失)"""
    if img is None:
        return False
    r1, g1, b1 = _px(img, 2450, 800)
    r2, g2, b2 = _px(img, 2180, 880)
    is_top_orange = r1 > 230 and 175 <= g1 <= 225 and b1 < 105
    is_bot_orange = r2 > 220 and 95 <= g2 <= 155 and b2 < 70
    return is_top_orange and is_bot_orange


def is_scout_screen_visible(img: Optional["Image.Image"]) -> bool:
    """檢查是否處於偵察對手畫面 (『結束戰鬥』與『下一個』同時存在)"""
    return is_end_battle_visible(img) and is_next_btn_visible(img)


def detect_return_home_btn(img: Optional["Image.Image"]) -> Optional[Tuple[float, float]]:
    """
    掃描結算畫面底部中央綠色『回營』按鈕，回傳實際比例座標 (rx, ry)。
    自動相容有無勝利之星橫幅造成之微幅 Y 位移，並要求底部存在結算黑邊以避免誤判。
    """
    if img is None or is_home_screen_visible(img) or is_end_battle_visible(img):
        return None
    br, bg, bb = _px(img, 1320, 1180)
    if not (br < 30 and bg < 30 and bb < 30):
        return None

    matched_ys = []
    for by in range(960, 1110, 6):
        r1, g1, b1 = _px(img, 1230, by)
        r2, g2, b2 = _px(img, 1410, by)
        g_ok1 = g1 > 170 and r1 > 85 and b1 < 110 and g1 > b1 + 75
        g_ok2 = g2 > 170 and r2 > 85 and b2 < 110 and g2 > b2 + 75
        if g_ok1 and g_ok2:
            matched_ys.append(by)

    if len(matched_ys) >= 4:
        mid_y = (matched_ys[0] + matched_ys[-1]) / 2.0
        return (1320 / BASE_W, mid_y / BASE_H)
    return None


def is_return_home_visible(img: Optional["Image.Image"]) -> bool:
    return detect_return_home_btn(img) is not None


# =====================================================================
# 內建極速 CoC 數字辨識 (可掠奪資源 & 卡牌 xN 數量)
# =====================================================================

def _find_connected_boxes(mask: "np.ndarray") -> List[Tuple[int, int, int, int, "np.ndarray"]]:
    """回傳二值遮罩中所有符合數字尺寸的連通區塊 (x, y, w, h, crop_mask)，由左至右排序。"""
    blobs = []
    if HAS_CV2:
        num, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        for i in range(1, num):
            x, y, w, h, _ = stats[i]
            if 14 <= h <= 36 and 5 <= w <= 32:
                blobs.append((int(x), int(y), int(w), int(h), mask[y:y + h, x:x + w]))
    else:
        # 純 NumPy 簡易 8-連通 BFS 保底 (無 cv2 環境亦可用)
        h_img, w_img = mask.shape
        visited = np.zeros_like(mask, dtype=bool)
        ys_nz, xs_nz = np.nonzero(mask)
        for sy, sx in zip(ys_nz, xs_nz):
            if visited[sy, sx]:
                continue
            stack = [(int(sy), int(sx))]
            visited[sy, sx] = True
            min_x = max_x = int(sx)
            min_y = max_y = int(sy)
            while stack:
                cy, cx = stack.pop()
                for ny in range(max(0, cy - 1), min(h_img, cy + 2)):
                    for nx in range(max(0, cx - 1), min(w_img, cx + 2)):
                        if mask[ny, nx] and not visited[ny, nx]:
                            visited[ny, nx] = True
                            stack.append((ny, nx))
                            if nx < min_x:
                                min_x = nx
                            if nx > max_x:
                                max_x = nx
                            if ny < min_y:
                                min_y = ny
                            if ny > max_y:
                                max_y = ny
            bw, bh = max_x - min_x + 1, max_y - min_y + 1
            if 14 <= bh <= 36 and 5 <= bw <= 32:
                blobs.append((min_x, min_y, bw, bh, mask[min_y:max_y + 1, min_x:max_x + 1]))

    blobs.sort(key=lambda b: b[0])
    return blobs


def _resize_mask_12x16(sub_mask: "np.ndarray") -> "np.ndarray":
    if HAS_CV2:
        resized = cv2.resize(sub_mask.astype(np.uint8) * 255, (12, 16), interpolation=cv2.INTER_AREA)
        return resized > 127
    pil_m = Image.fromarray((sub_mask.astype(np.uint8) * 255), mode="L").resize((12, 16), Image.BILINEAR)
    return np.asarray(pil_m) > 127


def _match_digit(w: int, h: int, sub_mask: "np.ndarray") -> Tuple[str, float]:
    """將單一字元區塊與 0-9 範本做 IoU 匹配，回傳 (最接近數字, IoU 分數)。"""
    norm = _resize_mask_12x16(sub_mask)
    ar = w / max(1, h)
    best_ch = ""
    best_iou = -1.0
    for ch, tmpl in DIGIT_TEMPLATES.items():
        if ch == "1" and ar > 0.56:
            continue
        if ch != "1" and ar < 0.46:
            continue
        inter = int(np.logical_and(norm, tmpl).sum())
        union = int(np.logical_or(norm, tmpl).sum())
        iou = inter / max(1, union)
        if iou > best_iou:
            best_iou = iou
            best_ch = ch
    return best_ch, best_iou


def _read_digits_from_mask(mask: "np.ndarray", min_iou: float = 0.52) -> Optional[int]:
    if not HAS_NUMPY or not DIGIT_TEMPLATES:
        return None
    blobs = _find_connected_boxes(mask)
    if not blobs:
        return None
    digits = []
    for _, _, w, h, sub in blobs:
        ch, score = _match_digit(w, h, sub)
        if ch and score >= min_iou:
            digits.append(ch)
    if not digits:
        return None
    try:
        return int("".join(digits))
    except ValueError:
        return None


def read_loot(img: Optional["Image.Image"]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    從偵察/戰鬥畫面左上角讀取可掠奪戰利品 (gold, elixir, dark_elixir)。
    實測 2640x1216 座標與字色：
    - 金幣 (Gold):       y=150..192, x=220..470, 淡黃白 RGB≈(255, 251, 204)
    - 聖水 (Elixir):     y=220..260, x=220..470, 淡粉白 RGB≈(255, 232, 253)
    - 闇黑重油 (Dark):   y=290..332, x=220..470, 純白   RGB≈(243, 243, 243)
    """
    arr = _to_base_rgb(img)
    if arr is None:
        return None, None, None

    g_roi = arr[150:192, 220:470]
    g_mask = (g_roi[:, :, 0] > 232) & (g_roi[:, :, 1] > 226) & (g_roi[:, :, 2] > 175)
    gold = _read_digits_from_mask(g_mask)

    e_roi = arr[220:260, 220:470]
    e_mask = (e_roi[:, :, 0] > 232) & (e_roi[:, :, 1] > 205) & (e_roi[:, :, 2] > 225)
    elixir = _read_digits_from_mask(e_mask)

    d_roi = arr[290:332, 220:470]
    d_mask = (d_roi[:, :, 0] > 218) & (d_roi[:, :, 1] > 218) & (d_roi[:, :, 2] > 218)
    dark = _read_digits_from_mask(d_mask)

    return gold, elixir, dark


def loot_meets_criteria(
    gold: Optional[int],
    elixir: Optional[int],
    dark: Optional[int],
    min_gold: int,
    min_elixir: int,
    min_dark: int,
    min_total: int,
) -> bool:
    """
    判斷目標戰利品是否達標：
    - 若無任何門檻 (>0)，直接回傳 True。
    - 闇黑重油若設定 min_dark > 0，必須達到 min_dark (若對面沒黑油庫辨識為 None 則視為 0)。
    - 金幣與聖水：滿足 (gold >= min_gold 且 elixir >= min_elixir) 或 (min_total > 0 且 gold + elixir >= min_total) 即達標。
    """
    if min_gold <= 0 and min_elixir <= 0 and min_dark <= 0 and min_total <= 0:
        return True
    if gold is None and elixir is None:
        return False

    g = gold or 0
    e = elixir or 0
    d = dark or 0

    if min_dark > 0 and d < min_dark:
        return False

    total_ok = min_total > 0 and (g + e) >= min_total
    indiv_ok = (g >= min_gold) and (e >= min_elixir)
    if min_gold > 0 or min_elixir > 0:
        return indiv_ok or total_ok
    if min_total > 0:
        return total_ok
    return True


def _read_card_count(arr: "np.ndarray", x1: int, x2: int) -> Optional[int]:
    """讀取卡牌右上角 `xN` 白色數量（自動跳過最左側較矮的字母 'x'）。"""
    rx1 = max(0, x2 - 80)
    rx2 = min(BASE_W, x2 + 4)
    roi = arr[1004:1045, rx1:rx2]
    mask = (roi[:, :, 0] > 228) & (roi[:, :, 1] > 228) & (roi[:, :, 2] > 228)
    blobs = _find_connected_boxes(mask)
    if len(blobs) < 2:
        return None
    # 最左邊的字元為小寫 'x'，其餘右側連通區塊即為數字位數
    digits = []
    for _, _, w, h, sub in blobs[1:]:
        ch, score = _match_digit(w, h, sub)
        if ch and score >= 0.48:
            digits.append(ch)
    if not digits:
        return None
    try:
        return max(1, int("".join(digits)))
    except ValueError:
        return None


# =====================================================================
# 通用動態底部兵種列掃描 & 存活／技能狀態偵測
# =====================================================================

def scan_army_cards(img: Optional["Image.Image"]) -> List[CardSlot]:
    """
    動態掃描底部兵種列 (y=1194 彩色卡牌底框) 找出所有可用卡牌並自動分類：
    - troop : 有右上角白色 xN 數量，且非紫色法術頂條
    - spell : 有右上角白色 xN 數量，且為亮紫色法術頂條 (RGB≈(118,85,222))
    - hero  : 無 xN 數量，底框為英雄紫藍 (RGB≈(118,102,240))
    - siege : 無 xN 數量，底框為攻城機器青藍 (RGB≈(79,165,185))
    """
    arr = _to_base_rgb(img)
    if arr is None:
        return []

    row = arr[1194, :, :].astype(int)
    sat = row.max(axis=1) - row.min(axis=1)
    is_card = (sat >= 35) & (row.max(axis=1) >= 95)

    runs: List[Tuple[int, int]] = []
    start = None
    gap = 0
    for x in range(150, 2490):
        if is_card[x]:
            if start is None:
                start = x
            gap = 0
        else:
            if start is not None:
                gap += 1
                if gap > 8:
                    end = x - gap
                    if 105 <= end - start <= 170:
                        runs.append((start, end))
                    start = None
                    gap = 0
    if start is not None:
        end = 2489
        if 105 <= end - start <= 170:
            runs.append((start, end))

    cards: List[CardSlot] = []
    for idx, (x1, x2) in enumerate(runs):
        cx = (x1 + x2) // 2
        b_rgb = arr[1194, max(0, cx - 20):min(BASE_W, cx + 20)].mean(axis=0)
        t_rgb = arr[1018, x1 + 8:min(BASE_W, x1 + 40)].mean(axis=0)
        cnt_roi = arr[1006:1042, max(0, x2 - 70):max(1, x2 - 2)]
        white_cnt = int(((cnt_roi[:, :, 0] > 232) & (cnt_roi[:, :, 1] > 232) & (cnt_roi[:, :, 2] > 232)).sum())

        mid = arr[1050:1150, max(0, cx - 35):min(BASE_W, cx + 35)]
        mid_sat = float((mid.max(axis=2) - mid.min(axis=2)).mean())
        active = mid_sat >= 28.0

        br, bg, bb = b_rgb
        tr, tg, tb = t_rgb
        if white_cnt >= 120:
            if tb > 175 and tr > 85 and tb > tg + 75:
                kind = "spell"
            else:
                kind = "troop"
            count = _read_card_count(arr, x1, x2) or 1
        else:
            if bb > 195 and br > 85 and bg < 135:
                kind = "hero"
            elif bg > 135 and bb > 150 and br < 115:
                kind = "siege"
            else:
                # 少見特殊卡保底視為兵種
                kind = "troop"
            count = 1

        cards.append(CardSlot(
            index=idx,
            x1=x1,
            x2=x2,
            cx=cx,
            kind=kind,
            count=count,
            active=active,
        ))
    return cards


def _is_slot_portrait_colored(img: "Image.Image", cx: int) -> bool:
    """檢查指定卡牌中心頭像區是否仍為彩色（用盡或陣亡後會變灰階）。"""
    max_sat = 0
    for dx in (-22, 0, 22):
        for dy in (1060, 1085, 1110, 1135):
            r, g, b = _px(img, cx + dx, dy)
            sat = max(r, g, b) - min(r, g, b)
            if sat > max_sat:
                max_sat = sat
    return max_sat >= 65


def is_hero_deployed_alive(img: Optional["Image.Image"], cx: int) -> bool:
    """檢查指定英雄槽位是否已下場且存活 (頭像彩色 + 卡牌上方出現綠色血條或上移邊框)"""
    if img is None or not _is_slot_portrait_colored(img, cx):
        return False
    # 已下場的英雄卡牌會上移並於頂部顯示亮綠色血條 (y ≈ 960..995)
    for dy in (962, 970, 978, 986):
        for dx in (-45, -20, 0, 20):
            r, g, b = _px(img, cx + dx, dy)
            if g > 165 and g > r + 40 and b < 85:
                return True
    return False


def is_card_still_undeployed(img: Optional["Image.Image"], card: CardSlot) -> bool:
    """檢查原偵測到的卡牌是否仍在牌組尚未用完/未下場。"""
    if img is None or not _is_slot_portrait_colored(img, card.cx):
        return False
    if card.kind == "hero":
        if is_hero_deployed_alive(img, card.cx):
            return False
        # 未下場英雄底框仍位於 y=1194 亮紫藍
        r, g, b = _px(img, card.cx, 1194)
        return b > 195 and r > 85 and g < 135
    if card.kind == "siege":
        r, g, b = _px(img, card.cx, 1194)
        return g > 135 and b > 150 and r < 115
    # troop / spell: 只要 y=1194 底框仍有色彩且右上角仍有白色 xN 數量，即尚未用盡
    r, g, b = _px(img, card.cx, 1194)
    sat = max(r, g, b) - min(r, g, b)
    return sat >= 35 and max(r, g, b) >= 95


# =====================================================================
# 外圍純草地安全下兵線偵測 + 紅框閃爍即時偵測與自動外推
# =====================================================================

def _build_ui_allowed_mask() -> "np.ndarray":
    """建立 UI 禁區遮罩（排除左上資源、右上資源、上方計時、左下結束戰鬥、右下下一個、底部兵種列）。"""
    ui_mask = np.ones((BASE_H, BASE_W), dtype=bool)
    ui_mask[0:360, 0:460] = False          # 左上角玩家與可掠奪資源欄
    ui_mask[0:290, 2000:BASE_W] = False    # 右上角自己資源欄
    ui_mask[0:120, 920:1720] = False       # 正上方倒數計時
    ui_mask[820:BASE_H, :] = False         # 左下結束戰鬥、強化橫幅、底部兵種列
    ui_mask[720:BASE_H, 2060:BASE_W] = False  # 右下「下一個」按鈕
    ui_mask[:, 0:45] = False
    ui_mask[:, BASE_W - 45:BASE_W] = False
    ui_mask[0:45, :] = False
    return ui_mask


def detect_home_deploy_points(
    img: Optional["Image.Image"],
    num_pts: int = 8,
    margin_from_building: int = 52,
) -> Tuple[Optional[List[Tuple[float, float]]], Optional[List[Tuple[float, float]]]]:
    """
    外緣射線掃描法 (Outer-Edge Ray-Cast)：
    從螢幕左/右邊界往陣型方向掃描，找到外圍森林/水域後的『第一段連續純草地帶』，
    一撞到敵方最外層建築/城牆立即停止，絕對不穿越進陣型內部空地或紅框禁區！
    回傳 (主要側安全下兵點列表, 另一側備用下兵點列表)。
    """
    arr = _to_base_rgb(img)
    if arr is None:
        return None, None

    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)

    # 明亮可部署草地遮罩 (排除深色森林 g<108、建築物、城牆、水面)
    is_grass = (g >= 108) & (g <= 220) & (g - r >= 10) & (g - b >= 40) & (b <= 105)
    grass_u8 = is_grass.astype(np.uint8) * 255

    if HAS_CV2:
        k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        clean_grass = cv2.morphologyEx(
            cv2.morphologyEx(grass_u8, cv2.MORPH_CLOSE, k_close),
            cv2.MORPH_OPEN,
            k_open,
        )
        dist_map = cv2.distanceTransform(clean_grass, cv2.DIST_L2, 5)
    else:
        clean_grass = grass_u8
        dist_map = (clean_grass > 0).astype(np.float32) * 30.0

    ui_mask = _build_ui_allowed_mask()

    def _scan_side(from_left: bool) -> List[Tuple[int, int, float]]:
        pts: List[Tuple[int, int, float]] = []
        for by in range(85, 815, 22):
            if from_left:
                x_start = 465 if by < 360 else 45
                xs_range = range(x_start, 1100)
            else:
                x_start = 1995 if by < 290 else (2055 if by > 720 else BASE_W - 45)
                xs_range = range(x_start, 1540, -1)

            in_grass = False
            seg_xs: List[int] = []
            non_grass_run = 0
            walked = 0
            found_seg: Optional[List[int]] = None

            for x in xs_range:
                walked += 1
                if clean_grass[by, x] > 0:
                    if not in_grass:
                        in_grass = True
                        seg_xs = [x]
                        non_grass_run = 0
                    else:
                        seg_xs.append(x)
                        non_grass_run = 0
                else:
                    if in_grass:
                        non_grass_run += 1
                        if non_grass_run >= 12:
                            if len(seg_xs) >= 26:
                                found_seg = seg_xs
                            break
                if not in_grass and walked > 380:
                    break
            else:
                if in_grass and len(seg_xs) >= 26:
                    found_seg = seg_xs

            if not found_seg:
                continue

            seg_arr = np.array(found_seg)
            dists = dist_map[by, seg_arr]
            best_x = int(seg_arr[int(np.argmax(dists))])
            inner_x = found_seg[-1]  # 靠敵方建築的那一側

            # 目標：距離最外層建築至少 margin_from_building (52px，約 1.5 格以上，完全在紅框外)，
            # 且若草地帶很寬，優先貼近內側安全邊緣（不跑到離陣型太遠的森林邊）
            if from_left:
                hug_x = max(found_seg[0] + 12, inner_x - margin_from_building)
                safe_x = hug_x if dist_map[by, hug_x] >= 22.0 else min(best_x, hug_x)
                safe_x = max(found_seg[0] + 10, min(found_seg[-1] - 12, safe_x))
            else:
                hug_x = min(found_seg[0] - 12, inner_x + margin_from_building)
                safe_x = hug_x if dist_map[by, hug_x] >= 22.0 else max(best_x, hug_x)
                safe_x = min(found_seg[0] - 10, max(found_seg[-1] + 12, safe_x))

            d_final = float(dist_map[by, safe_x])
            if ui_mask[by, safe_x] and d_final >= 18.0:
                pts.append((safe_x, by, d_final))
        return pts

    def _sample(pool: List[Tuple[int, int, float]]) -> Optional[List[Tuple[float, float]]]:
        if not pool:
            return None
        high = [p for p in pool if p[2] >= 24.0]
        use = high if len(high) >= 5 else pool
        idxs = np.linspace(0, len(use) - 1, min(num_pts, len(use))).astype(int)
        return [(use[i][0] / BASE_W, use[i][1] / BASE_H) for i in idxs]

    left_pts = _scan_side(from_left=True)
    right_pts = _scan_side(from_left=False)

    if len(left_pts) >= 4:
        return _sample(left_pts), _sample(right_pts)
    if len(right_pts) >= 4:
        return _sample(right_pts), _sample(left_pts)
    merged = left_pts + right_pts
    return _sample(merged), None


def detect_red_flash_outer_points(
    before_img: Optional["Image.Image"],
    after_img: Optional["Image.Image"],
    dist_out_px: int = 58,
    num_pts: int = 8,
) -> Tuple[bool, Optional[List[Tuple[float, float]]]]:
    """
    比對點擊前後兩張截圖，偵測是否觸發『紅框禁止部署閃爍』：
    - 若點擊落在紅框內，遊戲會在整個敵方陣型外圍亮起大量半透明紅色界線 (diff_R 急升且 diff_G 下降)。
    - 若偵測到紅框閃爍，直接取閃爍紅線的最外側輪廓往外推 dist_out_px 產生新下兵點！
    回傳 (是否偵測到紅框閃爍, 依紅線外推的新下兵點或 None)。
    """
    arr1 = _to_base_rgb(before_img)
    arr2 = _to_base_rgb(after_img)
    if arr1 is None or arr2 is None:
        return False, None

    # 只檢查地圖主區域 (排除上方倒數與下方兵種列/按鈕)
    y1, y2, x1, x2 = 120, 820, 80, BASE_W - 80
    a1 = arr1[y1:y2, x1:x2].astype(np.int16)
    a2 = arr2[y1:y2, x1:x2].astype(np.int16)

    dr = a2[:, :, 0] - a1[:, :, 0]
    dg = a2[:, :, 1] - a1[:, :, 1]
    red_flash = (dr >= 28) & (dg <= -10) & (a2[:, :, 0] >= 145) & (a2[:, :, 0] - a2[:, :, 1] >= 35)
    flash_count = int(red_flash.sum())

    # 正常下兵只會有一隻兵的局部小範圍變化；紅框閃爍會整圈陣型亮起數千個紅色像素
    if flash_count < 1200:
        return False, None

    ui_mask = _build_ui_allowed_mask()
    outer_pts: List[Tuple[int, int]] = []
    for by in range(140, 810, 26):
        row = red_flash[by - y1, :]
        xs = np.nonzero(row)[0]
        if len(xs) < 6:
            continue
        left_red_x = int(xs[0]) + x1
        cand_x = max(55, left_red_x - dist_out_px)
        if ui_mask[by, cand_x]:
            outer_pts.append((cand_x, by))

    if len(outer_pts) >= 4:
        idxs = np.linspace(0, len(outer_pts) - 1, min(num_pts, len(outer_pts))).astype(int)
        ratio_pts = [(outer_pts[i][0] / BASE_W, outer_pts[i][1] / BASE_H) for i in idxs]
        return True, ratio_pts
    return True, None


def probe_and_verify_deploy_points(
    first_card: CardSlot,
    scout_img: Optional["Image.Image"],
    primary_pts: List[Tuple[float, float]],
    alt_pts: List[Tuple[float, float]],
    screen_wh: Tuple[int, int],
    serial: Optional[str],
) -> Tuple[List[Tuple[float, float]], bool]:
    """
    先以第一張兵種卡做『單兵探針測試』，即時驗證是否點在紅框內：
    1. 選取 first_card 並只點 1 次候選下兵點中段。
    2. 立即截圖檢查：
       - 若偵測到紅框閃爍，或兵數完全沒減少 -> 立刻改用『紅線外推點』或『另一側備用外圍點』再驗證！
       - 若成功放下 (無紅框且數量減少) -> 立刻確認這組下兵線安全可用。
    回傳 (最終驗證通過的下兵點列表, first_card 是否已透過探針放下 1 隻)。
    """
    if scout_img is None or not primary_pts:
        return primary_pts, False

    candidates_sets = [primary_pts, alt_pts, COORDS_RATIO["deploy_line_left"], COORDS_RATIO["deploy_line_retry"]]
    cur_pts = primary_pts
    before_img = scout_img
    cnt_before = first_card.count

    for attempt in range(3):
        probe_pt = cur_pts[len(cur_pts) // 2]
        cmds = [
            make_tap_cmd(card_ratio_xy(first_card), screen_wh, jitter_px=4),
            "sleep 0.08",
            make_tap_cmd(probe_pt, screen_wh, jitter_px=4),
        ]
        run_adb(["shell", "; ".join(cmds)], serial)
        time.sleep(0.28)
        after_img = capture_screen_pil(serial)
        if after_img is None:
            return cur_pts, True

        # 檢查 1：是否觸發紅框閃爍
        flashed, red_outer_pts = detect_red_flash_outer_points(before_img, after_img, dist_out_px=58)

        # 檢查 2：該張兵卡數量是否減少（若原本是 x1 則檢查是否已變灰耗盡）
        deployed_ok = False
        if first_card.kind == "troop" and cnt_before > 1:
            arr_after = _to_base_rgb(after_img)
            cnt_after = _read_card_count(arr_after, first_card.x1, first_card.x2) if arr_after is not None else None
            if cnt_after is not None and cnt_after < cnt_before:
                deployed_ok = True
        else:
            deployed_ok = not is_card_still_undeployed(after_img, first_card)

        if deployed_ok and not flashed:
            px_x, px_y = int(probe_pt[0] * BASE_W), int(probe_pt[1] * BASE_H)
            print(f"  -> [紅框偵測] 探針下兵通過 ({px_x},{px_y})，確認位於紅框外安全草地！")
            return cur_pts, True

        reason = "偵測到紅框閃爍" if flashed else "探針兵種未放下(疑點進紅框)"
        if red_outer_pts:
            print(f"  -> [紅框偵測] {reason}！已依紅線輪廓自動外推 {len(red_outer_pts)} 個安全點重試...")
            cur_pts = red_outer_pts
        else:
            next_idx = min(attempt + 1, len(candidates_sets) - 1)
            cur_pts = candidates_sets[next_idx]
            print(f"  -> [紅框偵測] {reason}！自動切換備用外圍邊界 (組 #{next_idx + 1}) 重試...")
        before_img = after_img

    return cur_pts, False


# =====================================================================
# 點擊、批次下兵、法術與英雄技能控制
# =====================================================================

def make_tap_cmd(
    ratio_xy: Tuple[float, float],
    screen_wh: Tuple[int, int],
    jitter_px: int = 5,
) -> str:
    w, h = screen_wh
    cx = int(ratio_xy[0] * w) + random.randint(-jitter_px, jitter_px)
    cy = int(ratio_xy[1] * h) + random.randint(-jitter_px, jitter_px)
    return f"input tap {cx} {cy}"


def make_swipe_cmd(
    start_ratio: Tuple[float, float],
    end_ratio: Tuple[float, float],
    screen_wh: Tuple[int, int],
    duration_ms: int = 800,
) -> str:
    w, h = screen_wh
    x1, y1 = int(start_ratio[0] * w), int(start_ratio[1] * h)
    x2, y2 = int(end_ratio[0] * w), int(end_ratio[1] * h)
    return f"input swipe {x1} {y1} {x2} {y2} {duration_ms}"


def fast_tap(
    ratio_xy: Tuple[float, float],
    screen_wh: Tuple[int, int],
    jitter_px: int = 5,
    serial: Optional[str] = None,
    label: str = "",
):
    cmd_str = make_tap_cmd(ratio_xy, screen_wh, jitter_px)
    if label:
        print(f"    -> [Tap] {label}: {cmd_str}")
    run_adb(["shell", cmd_str], serial)


def card_ratio_xy(card: CardSlot) -> Tuple[float, float]:
    return (card.cx / BASE_W, CARD_TAP_Y / BASE_H)


def deploy_troops_and_heroes(
    cards: List[CardSlot],
    deploy_pts: List[Tuple[float, float]],
    screen_wh: Tuple[int, int],
    serial: Optional[str],
    point_offset: int = 0,
):
    """
    依序部署傳入的兵種 (troop)、攻城機器 (siege) 與英雄 (hero)：
    - 數量 <= 14 的兵種：沿下兵線均勻多點快點 (一字排開)，每張卡多補 1 點防止漏放。
    - 數量 > 14 的大量兵種 (如哥布林/野蠻人)：沿下兵線執行慢速滑動長按撒兵，極速清空。
    - 攻城機器與英雄：集中部署於下兵線中段跟隨大部隊推進。
    """
    troops = [c for c in cards if c.kind == "troop"]
    sieges = [c for c in cards if c.kind == "siege"]
    heroes = [c for c in cards if c.kind == "hero"]

    if not troops and not sieges and not heroes:
        return

    cmds: List[str] = []
    n_pts = len(deploy_pts)

    # 1. 部署所有兵種
    for t_idx, card in enumerate(troops):
        cmds.append(make_tap_cmd(card_ratio_xy(card), screen_wh, jitter_px=5))
        cmds.append("sleep 0.08")
        if card.count > 14:
            # 大量兵種：沿下兵線來回長按撒兵
            p_start = deploy_pts[0]
            p_end = deploy_pts[-1]
            dur_ms = min(2200, max(700, card.count * 45))
            cmds.append(make_swipe_cmd(p_start, p_end, screen_wh, duration_ms=dur_ms))
            cmds.append("sleep 0.12")
            if card.count > 35:
                cmds.append(make_swipe_cmd(p_end, p_start, screen_wh, duration_ms=dur_ms))
                cmds.append("sleep 0.12")
        else:
            # 中小數量兵種：沿下兵線均勻一字排開
            taps = card.count
            for k in range(taps):
                pt_idx = (k + t_idx + point_offset) % n_pts
                cmds.append(make_tap_cmd(deploy_pts[pt_idx], screen_wh, jitter_px=10))
                cmds.append("sleep 0.06")

    # 2. 部署攻城機器 (下兵線中段)
    mid_idx = n_pts // 2
    for card in sieges:
        pt = deploy_pts[(mid_idx + point_offset) % n_pts]
        cmds.append(make_tap_cmd(card_ratio_xy(card), screen_wh, jitter_px=5))
        cmds.append("sleep 0.08")
        cmds.append(make_tap_cmd(pt, screen_wh, jitter_px=8))
        cmds.append("sleep 0.10")

    # 3. 部署所有英雄 (圍繞下兵線中段展開)
    for h_idx, card in enumerate(heroes):
        pt = deploy_pts[(mid_idx + h_idx - len(heroes) // 2 + point_offset) % n_pts]
        cmds.append(make_tap_cmd(card_ratio_xy(card), screen_wh, jitter_px=5))
        cmds.append("sleep 0.08")
        cmds.append(make_tap_cmd(pt, screen_wh, jitter_px=8))
        cmds.append("sleep 0.08")

    summary_parts = []
    if troops:
        summary_parts.append("兵種[" + ",".join(f"x{c.count}" for c in troops) + "]")
    if sieges:
        summary_parts.append(f"攻城機器x{len(sieges)}")
    if heroes:
        summary_parts.append(f"英雄x{len(heroes)}")
    print(f"    -> [極速下兵] 部署 {' + '.join(summary_parts)}...")
    run_adb(["shell", "; ".join(cmds)], serial)


def deploy_spells_wave(
    spells: List[CardSlot],
    spell_pts: List[Tuple[float, float]],
    screen_wh: Tuple[int, int],
    serial: Optional[str],
    wave_idx: int = 0,
    drop_all: bool = False,
):
    """
    向陣型內部推進路線施放法術：
    - drop_all=False：每種法術本波施放至多 2 瓶（分波覆蓋部隊前進路線與冰凍防禦）。
    - drop_all=True ：將剩餘法術全數沿推進路線施放完畢。
    """
    if not spells:
        return
    cmds: List[str] = []
    n_pts = len(spell_pts)
    dropped_total = 0

    for s_idx, card in enumerate(spells):
        drops = card.count if drop_all else min(card.count, 2)
        if drops <= 0:
            continue
        cmds.append(make_tap_cmd(card_ratio_xy(card), screen_wh, jitter_px=5))
        cmds.append("sleep 0.08")
        for k in range(drops):
            pt = spell_pts[(wave_idx * 2 + s_idx + k) % n_pts]
            cmds.append(make_tap_cmd(pt, screen_wh, jitter_px=18))
            cmds.append("sleep 0.14")
            dropped_total += 1

    if cmds:
        tag = "收尾全下" if drop_all else f"第 {wave_idx + 1} 波"
        print(f"    -> [智慧法術] ({tag}) 施放 {dropped_total} 瓶法術...")
        run_adb(["shell", "; ".join(cmds)], serial)


def trigger_heroes_abilities(
    img: Optional["Image.Image"],
    heroes: List[CardSlot],
    screen_wh: Tuple[int, int],
    serial: Optional[str],
):
    """只點擊『已上場且仍存活』的英雄卡牌觸發裝備技能。"""
    if not heroes:
        return
    cmds: List[str] = []
    triggered = []
    for idx, card in enumerate(heroes):
        if img is None or is_hero_deployed_alive(img, card.cx):
            if cmds:
                cmds.append("sleep 0.05")
            cmds.append(make_tap_cmd(card_ratio_xy(card), screen_wh, jitter_px=5))
            triggered.append(f"英雄{idx + 1}")

    # 保底：若偵測未命中血條但卡牌仍彩色且已非未下場狀態，仍點擊一次
    if not cmds and img is not None:
        for idx, card in enumerate(heroes):
            if _is_slot_portrait_colored(img, card.cx) and not is_card_still_undeployed(img, card):
                if cmds:
                    cmds.append("sleep 0.05")
                cmds.append(make_tap_cmd(card_ratio_xy(card), screen_wh, jitter_px=5))
                triggered.append(f"英雄{idx + 1}(保底)")

    if cmds:
        print(f"    -> [英雄技能] 觸發 {', '.join(triggered)} 技能")
        run_adb(["shell", "; ".join(cmds)], serial)


# =====================================================================
# 單次畫面檢測診斷模式 (--inspect)
# =====================================================================

def inspect_current_screen(serial: Optional[str] = None):
    print("===================================================================")
    print("  CoC 多人戰鬥腳本 — 目前畫面診斷模式 (--inspect)")
    print("===================================================================")
    img = capture_screen_pil(serial)
    if img is None:
        print("[!] 無法擷取螢幕畫面，請確認 ADB 連線與 PIL 安裝。")
        return
    print(f"  -> 截圖解析度: {img.size[0]} x {img.size[1]}")

    states = []
    if is_home_screen_visible(img):
        states.append("家鄉主畫面 (Home)")
    if is_attack_modal_visible(img):
        states.append("多人遊戲進攻選單 (Attack Modal)")
    if is_army_confirm_visible(img):
        states.append("我的軍隊確認彈窗 (Army Confirm)")
    if is_scout_screen_visible(img):
        states.append("偵察對手畫面 (Scout)")
    elif is_end_battle_visible(img):
        states.append("戰鬥進行中 (Battle Active)")
    ret_btn = detect_return_home_btn(img)
    if ret_btn is not None:
        states.append(f"結算回營畫面 (Return Home @ y={int(ret_btn[1] * BASE_H)})")
    print(f"  -> 畫面狀態  : {', '.join(states) if states else '未知 / 過場白雲'}")

    if is_end_battle_visible(img):
        gold, elixir, dark = read_loot(img)
        print(f"  -> 可掠奪資源: 金幣={gold:,}" if gold is not None else "  -> 可掠奪資源: 金幣=None", end="")
        print(f" | 聖水={elixir:,}" if elixir is not None else " | 聖水=None", end="")
        print(f" | 黑油={dark:,}" if dark is not None else " | 黑油=None")

        cards = scan_army_cards(img)
        print(f"  -> 底部兵種列: 共偵測到 {len(cards)} 張卡牌")
        for c in cards:
            print(f"     Slot {c.index:2d}: kind={c.kind:5s} | count=x{c.count:<3d} | cx={c.cx:4d} | active={c.active}")

        main_pts, alt_pts = detect_home_deploy_points(img)
        if main_pts:
            print(
                f"  -> 外圍安全點: 共 {len(main_pts)} 點 "
                + ", ".join(f"({int(x * BASE_W)},{int(y * BASE_H)})" for x, y in main_pts)
            )
            if alt_pts:
                print(
                    f"  -> 備用對側點: 共 {len(alt_pts)} 點 "
                    + ", ".join(f"({int(x * BASE_W)},{int(y * BASE_H)})" for x, y in alt_pts[:4])
                    + " ..."
                )
        else:
            print("  -> 外圍安全點: 未偵測到外圍草地（將改用備用外緣點 + 紅框探針偵測）")


# =====================================================================
# 主多人戰鬥迴圈
# =====================================================================

def run_multiplayer_loop(
    rounds: int,
    mode: str = "normal",
    min_gold: int = 400000,
    min_elixir: int = 400000,
    min_dark: int = 0,
    min_total: int = 800000,
    max_next: int = 30,
    max_battle_sec: int = 195,
    hero_ability_delay: float = 8.0,
    serial: Optional[str] = None,
):
    print("===================================================================")
    print("  CoC 家鄉多人聯機戰鬥 (Multiplayer) 通用全自動腳本")
    print("===================================================================")
    print("  -> 等待 ADB 裝置連線...", flush=True)
    wait_cmd = ["adb"] + (["-s", serial] if serial else []) + ["wait-for-device"]
    try:
        subprocess.run(wait_cmd, timeout=10)
    except subprocess.TimeoutExpired:
        print("\n[!] 等待 ADB 裝置逾時 (10 秒)！目前 `adb devices` 狀態：")
        print(run_adb(["devices"]) or "(無輸出)")
        sys.exit(1)

    screen_wh = get_screen_landscape_size(serial)
    filter_enabled = any(v > 0 for v in (min_gold, min_elixir, min_dark, min_total))
    find_btn_key = "ranked_find_btn" if mode == "ranked" else "normal_find_btn"

    print(f"  -> 螢幕橫向解析度 : {screen_wh[0]} x {screen_wh[1]}")
    print(f"  -> 對戰模式       : {'排位戰 (Ranked)' if mode == 'ranked' else '一般戰鬥 (Normal)'}")
    print(f"  -> 智慧視覺偵測   : {'啟用 (Raw Screencap + 動態掃卡 + 內建資源辨識)' if (HAS_PIL and HAS_NUMPY) else '受限模式'}")
    if filter_enabled:
        print(
            f"  -> 挑魚條件       : (金>={min_gold:,} 且 水>={min_elixir:,})"
            + (f" 或 金+水>={min_total:,}" if min_total > 0 else "")
            + (f"，且 黑油>={min_dark:,}" if min_dark > 0 else "")
            + f" (上限換場 {max_next} 次)"
        )
    else:
        print("  -> 挑魚條件       : 關閉（搜到對手直接開打）")
    print(f"  -> 預計執行場數   : {rounds} 場 (按 Ctrl+C 可隨時停止)")
    print("===================================================================")

    # 若啟動時停在結算「回營」畫面，先點回村
    init_img = capture_screen_pil(serial)
    ret_btn = detect_return_home_btn(init_img)
    if ret_btn is not None:
        print("  -> 偵測到目前停在結算畫面，先點擊『回營』回到家鄉...")
        fast_tap(ret_btn, screen_wh, serial=serial, label="回營 (Return Home)")
        time.sleep(2.5)

    for r in range(1, rounds + 1):
        print(f"\n==================== [第 {r} / {rounds} 場多人戰鬥] ====================")

        # -----------------------------------------------------------------
        # Step 1~3: 狀態機進入配對 (家鄉「戰鬥」->「尋找對戰目標」->「攻擊！」-> 偵察畫面)
        # -----------------------------------------------------------------
        print("[Step 1] 進入多人配對流程...")
        scout_img = None
        t_enter = time.time()
        last_action_ts = 0.0

        while time.time() - t_enter < 30.0:
            img = capture_screen_pil(serial)
            now = time.time()

            if is_scout_screen_visible(img):
                scout_img = img
                print("  -> [智慧偵測] 已進入對手偵察畫面！")
                break

            if is_end_battle_visible(img) and not is_next_btn_visible(img):
                # 排位戰或已開始倒數進入戰鬥
                scout_img = img
                print("  -> [智慧偵測] 已進入戰鬥畫面！")
                break

            if now - last_action_ts >= 1.2:
                ret_btn = detect_return_home_btn(img)
                if ret_btn is not None:
                    fast_tap(ret_btn, screen_wh, serial=serial, label="回營 (Return Home)")
                    last_action_ts = now
                elif is_army_confirm_visible(img):
                    fast_tap(COORDS_RATIO["army_attack_btn"], screen_wh, serial=serial, label="我的軍隊：攻擊！")
                    last_action_ts = now
                elif is_attack_modal_visible(img):
                    fast_tap(COORDS_RATIO[find_btn_key], screen_wh, serial=serial, label="尋找對戰目標")
                    last_action_ts = now
                elif is_home_screen_visible(img):
                    fast_tap(COORDS_RATIO["attack_btn"], screen_wh, serial=serial, label="家鄉：戰鬥")
                    last_action_ts = now

            time.sleep(0.25)

        if scout_img is None:
            print("  [!] 進入對手偵察畫面逾時，嘗試關閉可能彈窗並進入下一輪。")
            fast_tap(COORDS_RATIO["star_bonus_ok_btn"], screen_wh, serial=serial, label="嘗試關閉彈窗")
            time.sleep(1.5)
            continue

        # -----------------------------------------------------------------
        # Step 2: 資源辨識 & 自動挑魚 (Nexting)
        # -----------------------------------------------------------------
        if filter_enabled and is_next_btn_visible(scout_img):
            print("[Step 2] 檢查對手可掠奪資源...")
            next_count = 0
            while next_count <= max_next:
                # 連續最多讀 2 次避免雲剛散數字還沒完全清晰
                gold, elixir, dark = read_loot(scout_img)
                if gold is None and elixir is None:
                    time.sleep(0.45)
                    scout_img = capture_screen_pil(serial) or scout_img
                    gold, elixir, dark = read_loot(scout_img)

                g_str = f"{gold:,}" if gold is not None else "?"
                e_str = f"{elixir:,}" if elixir is not None else "?"
                d_str = f"{dark:,}" if dark is not None else "?"
                tot_str = f"{(gold or 0) + (elixir or 0):,}" if (gold is not None or elixir is not None) else "?"
                print(f"  -> [目標 #{next_count + 1}] 金幣={g_str} | 聖水={e_str} (合計 {tot_str}) | 黑油={d_str}")

                if loot_meets_criteria(gold, elixir, dark, min_gold, min_elixir, min_dark, min_total):
                    print("  -> [挑魚達標] 資源符合條件，準備開打！")
                    break

                if next_count >= max_next:
                    print(f"  -> [達換場上限 {max_next} 次] 直接開打目前對手！")
                    break

                if not is_next_btn_visible(scout_img):
                    print("  -> [提示] 『下一個』按鈕已消失（準備時間結束），直接開打！")
                    break

                next_count += 1
                fast_tap(COORDS_RATIO["next_btn"], screen_wh, serial=serial, label=f"下一個 ({next_count}/{max_next})")

                # 等待白雲過場並偵測下一個對手出現
                time.sleep(1.5)
                t_next = time.time()
                loaded = False
                while time.time() - t_next < 14.0:
                    img = capture_screen_pil(serial)
                    if is_scout_screen_visible(img):
                        # 確認已換成新畫面 (稍微多等 0.25s 確保資源數字刷新)
                        time.sleep(0.25)
                        scout_img = capture_screen_pil(serial) or img
                        loaded = True
                        break
                    time.sleep(0.25)
                if not loaded:
                    scout_img = capture_screen_pil(serial) or scout_img
                    break

        # -----------------------------------------------------------------
        # Step 3: 動態掃描底部兵種列 + 紅線偵測 + 全自動一字排開下兵
        # -----------------------------------------------------------------
        print("[Step 3] 掃描底部兵種列並執行全自動部署...")
        cards = scan_army_cards(scout_img)
        if not cards:
            time.sleep(0.5)
            scout_img = capture_screen_pil(serial) or scout_img
            cards = scan_army_cards(scout_img)

        troops = [c for c in cards if c.kind == "troop"]
        sieges = [c for c in cards if c.kind == "siege"]
        heroes = [c for c in cards if c.kind == "hero"]
        spells = [c for c in cards if c.kind == "spell"]
        print(
            f"  -> [動態掃卡] 兵種 {len(troops)} 格 "
            f"({', '.join(f'x{c.count}' for c in troops) or '無'}) | "
            f"攻城機器 {len(sieges)} 格 | 英雄 {len(heroes)} 格 | "
            f"法術 {len(spells)} 格 ({', '.join(f'x{c.count}' for c in spells) or '無'})"
        )

        # 先用外緣射線掃描法從偵察圖找出「完全在紅框外」的外圍純草地下兵線（自動相容縮放與大小陣）
        deploy_pts = COORDS_RATIO["deploy_line_left"]
        alt_deploy_pts = COORDS_RATIO["deploy_line_retry"]
        if HAS_PIL and scout_img is not None:
            detected_main, detected_alt = detect_home_deploy_points(scout_img, num_pts=8, margin_from_building=52)
            if detected_main:
                deploy_pts = detected_main
                if detected_alt:
                    alt_deploy_pts = detected_alt
                print(
                    f"  -> [外圍草地偵測] 找到 {len(deploy_pts)} 個紅框外安全下兵點: "
                    + ", ".join(f"({int(x * BASE_W)},{int(y * BASE_H)})" for x, y in deploy_pts[:4])
                    + " ..."
                )
            else:
                print("  -> [外圍草地偵測] 非標準草地場景，改用備用最外圍邊界 + 紅框探針偵測。")

        # 單兵探針驗證：先下 1 隻確認沒點到紅框（若觸發紅框閃爍會自動依紅線外推或切對側）
        first_unit_list = troops or sieges or heroes
        if first_unit_list and HAS_PIL and scout_img is not None:
            probe_card = first_unit_list[0]
            deploy_pts, probe_dropped = probe_and_verify_deploy_points(
                first_card=probe_card,
                scout_img=scout_img,
                primary_pts=deploy_pts,
                alt_pts=alt_deploy_pts,
                screen_wh=screen_wh,
                serial=serial,
            )
            if probe_dropped:
                if probe_card.kind == "troop" and probe_card.count > 1:
                    probe_card.count -= 1
                elif probe_card.kind in ("siege", "hero") or probe_card.count <= 1:
                    first_unit_list.pop(0)

        # 主力下兵：所有剩餘兵種 + 攻城機器 + 英雄
        deploy_troops_and_heroes(
            troops + sieges + heroes,
            deploy_pts=deploy_pts,
            screen_wh=screen_wh,
            serial=serial,
            point_offset=0,
        )
        battle_start_ts = time.time()
        last_deploy_ts = battle_start_ts
        last_ability_ts = battle_start_ts
        last_spell_ts = battle_start_ts
        spell_wave = 0
        retry_count = 0

        # -----------------------------------------------------------------
        # Step 4: 高頻監控戰鬥 (補放漏兵 + 分波法術 + 智慧開英雄技能 + 偵測回營)
        # -----------------------------------------------------------------
        print("[Step 4] 監控戰鬥進度 (自動補兵、分波法術、智慧英雄技能、秒回營)...")
        ret_btn_pos: Optional[Tuple[float, float]] = None

        while time.time() - battle_start_ts < max_battle_sec:
            time.sleep(0.45)
            img = capture_screen_pil(serial)
            now = time.time()
            elapsed = now - battle_start_ts

            # A. 檢查結算「回營」按鈕
            ret_btn_pos = detect_return_home_btn(img)
            if ret_btn_pos is not None:
                print("  -> [智慧偵測] 戰鬥結束！偵測到『回營』按鈕！")
                break
            if is_home_screen_visible(img):
                print("  -> [智慧偵測] 已回到家鄉主畫面！")
                break

            # B. 檢查是否有漏放／未清空的兵種、攻城機器、英雄 (最多補兵 4 次)
            if img is not None and retry_count < 4 and (now - last_deploy_ts >= 1.5):
                undep_units = [c for c in (troops + sieges + heroes) if is_card_still_undeployed(img, c)]
                if undep_units:
                    retry_count += 1
                    # 重新讀取剩餘兵數，避免已用完大半的卡重複點太多次
                    arr_now = _to_base_rgb(img)
                    if arr_now is not None:
                        for c in undep_units:
                            if c.kind == "troop":
                                new_cnt = _read_card_count(arr_now, c.x1, c.x2)
                                if new_cnt is not None:
                                    c.count = new_cnt
                    print(
                        f"  -> [自動補兵 #{retry_count}] 仍有 {len(undep_units)} 格未下完 "
                        f"({', '.join(f'{c.kind}:x{c.count}' for c in undep_units)})，換點補放！"
                    )
                    alt_pts = alt_deploy_pts if retry_count % 2 == 1 else deploy_pts
                    deploy_troops_and_heroes(
                        undep_units,
                        deploy_pts=alt_pts,
                        screen_wh=screen_wh,
                        serial=serial,
                        point_offset=retry_count * 2,
                    )
                    last_deploy_ts = time.time()
                    continue

            # C. 分波施放法術 (開戰 3.5s 後開始第 1 波，之後每 4.5s 放下一波，第 3 波收尾全下)
            if spells and img is not None and elapsed >= 3.5 and (now - last_spell_ts >= 4.5):
                rem_spells = [c for c in spells if is_card_still_undeployed(img, c)]
                if rem_spells:
                    arr_now = _to_base_rgb(img)
                    if arr_now is not None:
                        for c in rem_spells:
                            new_cnt = _read_card_count(arr_now, c.x1, c.x2)
                            if new_cnt is not None:
                                c.count = new_cnt
                    deploy_spells_wave(
                        rem_spells,
                        spell_pts=COORDS_RATIO["spell_points"],
                        screen_wh=screen_wh,
                        serial=serial,
                        wave_idx=spell_wave,
                        drop_all=(spell_wave >= 2),
                    )
                    spell_wave += 1
                    last_spell_ts = time.time()

            # D. 智慧觸發存活英雄技能 (開戰 hero_ability_delay 秒後，每 5 秒檢查一次)
            if heroes and elapsed >= hero_ability_delay and (now - last_ability_ts >= 5.0):
                trigger_heroes_abilities(img, heroes, screen_wh, serial)
                last_ability_ts = time.time()

        # -----------------------------------------------------------------
        # Step 5: 點擊「回營」返回家鄉，並關閉可能出現的勝利之星獎勵視窗
        # -----------------------------------------------------------------
        if ret_btn_pos is None:
            # 若逾時仍在戰鬥畫面，先點「結束戰鬥」+「確定」
            cur_img = capture_screen_pil(serial)
            if is_end_battle_visible(cur_img):
                print("  -> [逾時收尾] 點擊『結束戰鬥』與『確定』...")
                fast_tap(COORDS_RATIO["end_battle_btn"], screen_wh, serial=serial, label="結束戰鬥")
                time.sleep(0.8)
                fast_tap(COORDS_RATIO["surrender_ok_btn"], screen_wh, serial=serial, label="確認結束")
                time.sleep(2.0)
                ret_btn_pos = detect_return_home_btn(capture_screen_pil(serial))

        if not is_home_screen_visible(capture_screen_pil(serial)):
            tap_pos = ret_btn_pos or COORDS_RATIO["return_home_btn"]
            print("[Step 5] 點擊『回營 (Return Home)』返回家鄉村莊...")
            fast_tap(tap_pos, screen_wh, serial=serial, label="回營 (Return Home)")

            t_home = time.time()
            bonus_clicked = False
            while time.time() - t_home < 7.0:
                time.sleep(0.3)
                img = capture_screen_pil(serial)
                if is_home_screen_visible(img):
                    break
                cur_ret = detect_return_home_btn(img)
                if cur_ret is not None:
                    fast_tap(cur_ret, screen_wh, serial=serial)
                elif not bonus_clicked and time.time() - t_home >= 2.4:
                    fast_tap(COORDS_RATIO["star_bonus_ok_btn"], screen_wh, serial=serial, label="關閉獎勵彈窗(若有)")
                    bonus_clicked = True

    print("\n===================================================================")
    print(f"  已完成全部 {rounds} 場家鄉多人聯機自動戰鬥！")
    print("===================================================================")


def main():
    parser = argparse.ArgumentParser(
        description="CoC 家鄉多人聯機戰鬥通用全自動腳本 (含資源辨識挑魚與動態掃卡)"
    )
    parser.add_argument("-n", "--rounds", type=int, default=15, help="連續執行的戰鬥場數 (預設: 15)")
    parser.add_argument("-s", "--serial", type=str, default=None, help="指定 ADB 裝置序號")
    parser.add_argument(
        "--mode",
        choices=["normal", "ranked"],
        default="normal",
        help="對戰模式：normal=一般戰鬥 (預設), ranked=排位戰",
    )
    parser.add_argument("--min-gold", type=int, default=400000, help="最低金幣門檻 (預設: 400000)")
    parser.add_argument("--min-elixir", type=int, default=400000, help="最低聖水門檻 (預設: 400000)")
    parser.add_argument("--min-dark", type=int, default=0, help="最低闇黑重油門檻 (預設: 0 不限)")
    parser.add_argument("--min-total", type=int, default=800000, help="金+水總和達標門檻 (預設: 800000；設 0 停用)")
    parser.add_argument("--max-next", type=int, default=30, help="單場最多點『下一個』次數 (預設: 30)")
    parser.add_argument("--no-filter", action="store_true", help="關閉資源挑魚，搜到對手立刻開打")
    parser.add_argument("--max-sec", type=int, default=195, help="單場最長戰鬥秒數 (預設: 195 秒)")
    parser.add_argument("--hero-delay", type=float, default=8.0, help="下兵後幾秒開始觸發英雄技能 (預設: 8.0 秒)")
    parser.add_argument("--inspect", action="store_true", help="僅截圖診斷目前畫面狀態、資源數字與兵種列，不點擊")
    args = parser.parse_args()

    if args.inspect:
        inspect_current_screen(serial=args.serial)
        return

    min_gold = 0 if args.no_filter else args.min_gold
    min_elixir = 0 if args.no_filter else args.min_elixir
    min_dark = 0 if args.no_filter else args.min_dark
    min_total = 0 if args.no_filter else args.min_total

    try:
        run_multiplayer_loop(
            rounds=args.rounds,
            mode=args.mode,
            min_gold=min_gold,
            min_elixir=min_elixir,
            min_dark=min_dark,
            min_total=min_total,
            max_next=args.max_next,
            max_battle_sec=args.max_sec,
            hero_ability_delay=args.hero_delay,
            serial=args.serial,
        )
    except KeyboardInterrupt:
        print("\n[!] 已收到 Ctrl+C，腳本安全停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
