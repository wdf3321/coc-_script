#!/usr/bin/env python3
"""
Clash of Clans (CoC) 夜世界 (Builder Base) 暗夜女巫 (Night Witch) 自動打星/刷資源腳本 (極速精準優化版)
已針對 OPPO CPH2723 (2640x1216) 實機重新校準 (原 Pixel 10 Pro XL 2404x1080 版)。

優化重點：
1. 【徹底解決兵種沒放下去的問題】：
   - 修正卡牌點擊 Y 座標：原本女巫卡牌點在 y=875 (剛好壓在未出戰卡牌頂部白線 y=883 之外)、英雄點在 y=905 (頂部邊緣)，
     改為卡牌正中央頭像區 (英雄 y=960、女巫 y=945)，100% 穩定選中卡牌且完美避開底部 y=1003~1045 的綠色換兵鈕。
   - 修正二階段 (Stage 2) 偵測與一階段存活女巫識別：
     * 原本檢查 (1555, 955) 誤點在女巫深色兜帽上導致永遠回傳 False；現改為檢查第 7、8 格未出戰紫色頂條 (y=905) 與右下角綠色換兵鈕 (y=1025)。
     * 新增識別「第一階段存活且已開過技能、進入第二階段等待重新下場」的女巫卡牌 (低位深藍頂條 RGB≈(18,36,75))，確保二階段連同存活女巫與第 7、8 格增援全數下場！
   - 改用單次 `input tap` 取代 `input swipe` 與雙擊地圖：
     * 避免 `input swipe` 在戰場地圖與底部兵種列被遊戲誤判為拖曳鏡頭或滑動兵種列。
     * 由於 CoC 下完一隻兵會自動選取下一張未下場卡牌，每選一張卡只點一次地圖，避免多點地圖誤把下一隻兵提前下掉而誤開技能。
   - 逐格智慧檢查補下兵 (Auto-Retry Undeployed)：每次畫面巡檢都會逐格檢查英雄與第 1~8 格女巫是否仍在牌組未下場，
     只要發現任何一格沒放下去，立刻針對「尚未下場的格子」精準補放！
2. 【大幅提升反應速度】：
   - 螢幕截圖改用未壓縮原始流 `adb exec-out screencap` + `Image.frombuffer` 零拷貝解析，
     截圖耗時從 ~2.3 秒大幅縮短至 ~0.9 秒。
   - 狀態機自癒點擊：點擊「戰鬥」與「立即尋找！」皆具備視覺確認與自動重試，白雲一散開立刻下兵；結算「回營」一出現立刻秒點回村。
   - 技能觸發智慧過濾：只點擊「已上場且技能亮起 (或英雄存活)」的卡牌，不浪費時間點已陣亡或冷卻中的格子。
"""

import argparse
import io
import random
import re
import struct
import subprocess
import sys
import time
from typing import List, Optional, Tuple

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# =====================================================================
# 以 OPPO CPH2723 (2640 x 1216) 實機校準之基準比例座標
# (原 Pixel 10 Pro XL 2404x1080 版本備份於 coc_night_witch_bot_pixel_backup.py)
# =====================================================================
BASE_W, BASE_H = 2640, 1216

# 英雄卡牌中心 x (卡牌範圍 x=384..531)
HERO_BASE_X = 457
# 第 1~8 格兵種卡牌中心 x (實測第 1~8 格等級徽章與中心點位置)
WITCH_BASE_XS = [650, 821, 992, 1163, 1334, 1505, 1675, 1839]
# 卡牌點擊高度 (卡牌頭像正中央，避開上方技能條與下方換兵鈕)
CARD_TAP_Y = 1080

COORDS_RATIO = {
    # 1. 夜世界主畫面左下角「戰鬥」(Attack) 按鈕 (黃框 x=154..364, y=978..1183)
    "attack_btn": (259 / BASE_W, 1080 / BASE_H),

    # 2. 開始進攻視窗右側綠色「立即尋找！」(Find Now) 按鈕 (綠框 x=1638..2053, y=729..867)
    "find_now_btn": (1845 / BASE_W, 798 / BASE_H),

    # 3. 備用下兵點 (僅在『地圖紅線與棋盤邊界偵測』皆失敗時使用，偏左側外側避免點進紅線)
    "deploy_points": [
        (460 / BASE_W, 580 / BASE_H),
        (500 / BASE_W, 540 / BASE_H),
        (430 / BASE_W, 610 / BASE_H),
        (520 / BASE_W, 520 / BASE_H),
    ],
    "stage2_deploy_points": [
        (480 / BASE_W, 560 / BASE_H),
        (520 / BASE_W, 520 / BASE_H),
        (450 / BASE_W, 590 / BASE_H),
        (560 / BASE_W, 500 / BASE_H),
        (420 / BASE_W, 540 / BASE_H),
        (540 / BASE_W, 580 / BASE_H),
    ],

    # 4. 底部兵種列卡牌正中央頭像區
    "slot_hero": (HERO_BASE_X / BASE_W, CARD_TAP_Y / BASE_H),
    "slot_witches": [(x / BASE_W, CARD_TAP_Y / BASE_H) for x in WITCH_BASE_XS],

    # 5. 結算畫面正下方綠色「回營」(Return Home) 按鈕 (綠框 x=1182..1458, y=974..1079)
    "return_home_btn": (1320 / BASE_W, 1026 / BASE_H),

    # 6. 每日星數獎勵彈窗「確認 (OK)」按鈕 (由舊版比例換算，尚未實測)
    "star_bonus_ok_btn": (1320 / BASE_W, 968 / BASE_H),
}


def run_adb(args: list, serial: Optional[str] = None) -> str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.stdout.strip()


def capture_screen_pil(serial: Optional[str] = None) -> Optional["Image.Image"]:
    """
    透過未壓縮 `adb exec-out screencap` 零拷貝快速轉為 PIL Image (~0.9s，比 screencap -p 快 2.5 倍)。
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


# =====================================================================
# 畫面狀態與逐格卡牌像素偵測
# =====================================================================

def _is_purple_bar(r: int, g: int, b: int) -> bool:
    """亮紫色技能條 (未下場低位 / 已下場高位)"""
    return r > 175 and b > 215 and g < 165 and b > g + 70


def _is_level_badge(r: int, g: int, b: int) -> bool:
    """存活單位左下角等級徽章：一般為亮青藍色，強化藥水狀態為紫色 (陣亡則變灰階)"""
    is_blue_badge = b > 170 and g > 130 and b > r + 75
    is_purple_badge = r > 105 and b > 120 and b > g + 35
    return is_blue_badge or is_purple_badge


def is_home_screen_visible(img: Optional["Image.Image"]) -> bool:
    """檢查是否在夜世界主村莊畫面 (左下角亮黃色『戰鬥』按鈕未受彈窗遮暗)"""
    if img is None:
        return False
    r1, g1, b1 = _px(img, 259, 1000)
    r2, g2, b2 = _px(img, 180, 1150)
    return (r1 > 220 and g1 > 175 and b1 < 50) and (r2 > 220 and g2 > 165 and b2 < 50)


def is_find_now_modal_visible(img: Optional["Image.Image"]) -> bool:
    """檢查是否已跳出『開始進攻』彈窗 (右側亮綠色『立即尋找！』按鈕 + 中央米白底板)"""
    if img is None:
        return False
    r, g, b = _px(img, 1700, 798)
    mr, mg, mb = _px(img, 1320, 340)
    return (g > 190 and r > 120 and g > b + 80) and (mr > 215 and mg > 210 and mb > 195)


def is_return_home_visible(img: Optional["Image.Image"]) -> bool:
    """檢查結算畫面正下方綠色『回營』按鈕 (左右兩點) 與下方黑邊 (y=1150) 是否出現"""
    if img is None:
        return False
    r1, g1, b1 = _px(img, 1230, 1026)
    r2, g2, b2 = _px(img, 1410, 1026)
    br, bg, bb = _px(img, 1320, 1150)
    is_green = (g1 > 175 and r1 > 90 and b1 < 100 and g1 > b1 + 80) and \
               (g2 > 175 and r2 > 90 and b2 < 100 and g2 > b2 + 80)
    return is_green and (br < 25 and bg < 25 and bb < 25)


def _is_portrait_colored(img: "Image.Image", cx: int) -> bool:
    """檢查卡牌頭像區是否為彩色（存活單位有豐富色彩；陣亡會變純灰階 max_sat<=5；空槽位背景飽和度低）"""
    max_sat = 0
    for dx in (-20, 0, 20):
        for dy in (1060, 1080, 1100, 1120):
            r, g, b = _px(img, cx + dx, dy)
            sat = max(r, g, b) - min(r, g, b)
            if sat > max_sat:
                max_sat = sat
    return max_sat >= 70


def _is_hero_badge_alive(img: "Image.Image") -> bool:
    if not _is_portrait_colored(img, HERO_BASE_X):
        return False
    for hx, hy in [(410, 1150), (400, 1180)]:
        r, g, b = _px(img, hx, hy)
        if _is_level_badge(r, g, b) or (r < 55 and g < 55 and b < 55):
            return True
    return False


def is_hero_deployed_alive(img: Optional["Image.Image"]) -> bool:
    """檢查英雄是否已下場且存活 (卡牌上移，y=970 出現亮綠色血條)"""
    if img is None or not _is_hero_badge_alive(img):
        return False
    rh, gh, bh = _px(img, 410, 970)
    ra, ga, ba = _px(img, 410, 950)
    rb, gb, bb = _px(img, HERO_BASE_X, 982)
    has_hp = gh > 170 and gh > rh + 40 and bh < 60
    has_ability_bar = ba > 180 and (ra > 150 or ga > 130)
    has_black_divider = rb < 20 and gb < 20 and bb < 20
    return has_hp or (has_ability_bar and has_black_divider)


def is_hero_undeployed(img: Optional["Image.Image"]) -> bool:
    """
    檢查英雄是否尚在牌組未下場：
    未下場時卡牌位於低位，右下角 (485, 1170) 有亮綠色換兵鈕，且卡牌下緣 (457, 1190) 為黑色陰影。
    """
    if img is None or not _is_hero_badge_alive(img) or is_hero_deployed_alive(img):
        return False
    sr, sg, sb = _px(img, 485, 1170)
    br, bg, bb = _px(img, HERO_BASE_X, 1190)
    has_swap = sg > 165 and sg > sb + 60 and sb < 120
    has_black_shadow = br < 25 and bg < 25 and bb < 25
    return has_swap or has_black_shadow


def is_witch_alive(img: Optional["Image.Image"], idx: int) -> bool:
    """
    檢查第 idx (0..7) 格是否存活：
    頭像必須為彩色 (排除陣亡灰階卡與空槽位)，且左下角等級徽章為一般黑底或強化藍/紫底。
    """
    if img is None or not (0 <= idx < len(WITCH_BASE_XS)):
        return False
    cx = WITCH_BASE_XS[idx]
    if not _is_portrait_colored(img, cx):
        return False
    for dx, dy in [(-25, 1170), (-32, 1165), (-18, 1175), (-25, 1160)]:
        r, g, b = _px(img, cx + dx, dy)
        if _is_level_badge(r, g, b) or max(r, g, b) < 35:
            return True
    return False


def is_witch_undeployed(img: Optional["Image.Image"], idx: int) -> bool:
    """
    檢查第 idx (0..7) 格是否仍在底部牌組未下場 (任一成立即可)：
    1. 卡牌下緣 (cx, 1188/1190/1192) 為亮藍色邊框 (下場後變綠色血條底 / 陣亡變灰)；
    2. 低位 (cx+27, 1020) 為亮紫色技能條，或一階段已開過技能的深藍色頂條；
    3. 右下 (cx+10, 1170) 有亮綠色換兵鈕。
    """
    if img is None or not (0 <= idx < len(WITCH_BASE_XS)):
        return False
    if not is_witch_alive(img, idx):
        return False
    cx = WITCH_BASE_XS[idx]
    is_blue_bottom = any(
        bb > 200 and bg > 125 and bb > br + 80
        for br, bg, bb in (_px(img, cx, y) for y in (1188, 1190, 1192))
    )
    pr, pg, pb = _px(img, cx + 27, 1020)
    is_low_purple = _is_purple_bar(pr, pg, pb)
    is_low_dark_blue = pr < 45 and pg < 65 and pb > 55 and pb > pr + 20
    sr, sg, sb = _px(img, cx + 10, 1170)
    has_swap = sg > 165 and sg > sb + 70 and sb < 120
    return is_blue_bottom or is_low_purple or is_low_dark_blue or has_swap


def is_witch_ability_ready(img: Optional["Image.Image"], idx: int) -> bool:
    """檢查第 idx 格是否『已下場』且『技能就緒 (亮紫色技能條上移至 y=975)』"""
    if img is None or not (0 <= idx < len(WITCH_BASE_XS)):
        return False
    if not is_witch_alive(img, idx):
        return False
    px = WITCH_BASE_XS[idx] + 27
    top_purple = _is_purple_bar(*_px(img, px, 975))
    low_purple = _is_purple_bar(*_px(img, px, 1020))
    return top_purple and not low_purple


def get_undeployed_witches(img: Optional["Image.Image"], max_slots: int) -> List[int]:
    """回傳 0..max_slots-1 中所有尚未下場的女巫 index 列表"""
    if img is None:
        return []
    return [i for i in range(max_slots) if is_witch_undeployed(img, i)]


def is_stage1_ready(img: Optional["Image.Image"]) -> bool:
    """檢查是否已載入第一階段戰場畫面 (前 6 格女巫至少有 2 格未下場或英雄未下場)"""
    if img is None:
        return False
    return is_hero_undeployed(img) or len(get_undeployed_witches(img, 6)) >= 2


def is_stage2_ready(img: Optional["Image.Image"]) -> bool:
    """檢查是否已進入第二階段準備畫面：第 7 或第 8 格增援女巫出現未下場狀態"""
    if img is None:
        return False
    return is_witch_undeployed(img, 6) or is_witch_undeployed(img, 7)


# =====================================================================
# 地圖偵測：找出紅色禁止部署線，在紅線外側的地磚/洞穴地面上計算下兵點
# =====================================================================

# 掃描範圍 (基準座標)：地圖左半部，避開上方玩家名稱與下方「已強化」橫幅 / 兵種列
MAP_SCAN_Y = (300, 790)
MAP_SCAN_X = (200, 1300)
MAP_SCAN_STEP_Y = 6


def _is_red_line_px(r: int, g: int, b: int) -> bool:
    """紅色禁止部署線 (實測約 RGB(157, 64, 51))"""
    return r > 125 and r - g > 55 and r - b > 65


def _is_tile_px(r: int, g: int, b: int) -> bool:
    """紅線外可部署的青綠色地磚 (實測約 RGB(75, 112, 95))，與深綠草地 RGB(40, 73, 44) 區分"""
    return g > 88 and b > r + 8 and g > r + 15


def _is_cave_ground_px(r: int, g: int, b: int) -> bool:
    """第二階段洞穴外側可部署暗色石地/灰綠地面 (非紅線、非高飽和亮色建築)"""
    return (not _is_red_line_px(r, g, b)) and r < 135 and g < 140 and b < 145


def detect_red_line_rows(
    img: Optional["Image.Image"],
    stage2_cave: bool = False,
) -> List[Tuple[int, int]]:
    """
    逐列由左往右掃描，回傳每一列『第一個紅線像素』的基準座標 (red_x, y)。
    為避免誤判紅色建築，要求紅線左側 (外側) 必須是可部署地面。
    """
    if img is None:
        return []
    rgb = img.convert("RGB")
    pix = rgb.load()
    w, h = rgb.size
    sx, sy = w / BASE_W, h / BASE_H

    def _scan(ground_fn) -> List[Tuple[int, int]]:
        found = []
        for by in range(MAP_SCAN_Y[0], MAP_SCAN_Y[1], MAP_SCAN_STEP_Y):
            y = int(by * sy)
            for bx in range(MAP_SCAN_X[0], MAP_SCAN_X[1]):
                x = int(bx * sx)
                if not _is_red_line_px(*pix[x, y][:3]):
                    continue
                outside = sum(
                    ground_fn(*pix[max(0, x - int(d * sx)), y][:3]) for d in (8, 14, 20)
                )
                if outside >= 2:
                    found.append((bx, by))
                    break
        return _filter_continuous_rows(found)

    rows = _scan(_is_tile_px)
    if stage2_cave and len(rows) < 12:
        cave_rows = _scan(_is_cave_ground_px)
        if len(cave_rows) > len(rows):
            return cave_rows
    return rows


def _filter_continuous_rows(rows: List[Tuple[int, int]], min_run: int = 6) -> List[Tuple[int, int]]:
    """
    紅線是連續的斜線，相鄰掃描列的 x 差距很小；特效、血條等零散紅色則不連續。
    只保留『連續長度 >= min_run 列』的線段，濾掉誤判。
    """
    runs, cur = [], []
    for x, y in rows:
        if cur and (y - cur[-1][1] > MAP_SCAN_STEP_Y or abs(x - cur[-1][0]) > 12):
            runs.append(cur)
            cur = []
        cur.append((x, y))
    if cur:
        runs.append(cur)
    return [p for run in runs if len(run) >= min_run for p in run]


def detect_deploy_points(
    img: Optional["Image.Image"],
    dist_px: int = 42,
    stage2_cave: bool = False,
) -> Optional[List[Tuple[float, float]]]:
    """
    依紅線找出地圖西側最突出的角 (紅線最左點 rx_min, y_vertex)，
    一律以 rx_min 左側 dist_px 為基準產生 3 個下兵點，確保不會因紅線凹角或斜率而點進紅線內。
    偵測不到紅線 (例如雲還沒散) 時回傳 None。
    """
    rows = detect_red_line_rows(img, stage2_cave=stage2_cave)
    if len(rows) < 12:
        return None
    rx_min, y_vertex = min(rows)
    base_x = rx_min - dist_px
    if base_x < MAP_SCAN_X[0]:
        base_x = max(80, rx_min - dist_px)
    points = []
    for dy, extra_out in ((0, 0), (-30, 12), (30, 12)):
        px_x = max(80, base_x - extra_out)
        px_y = min(MAP_SCAN_Y[1], max(MAP_SCAN_Y[0], y_vertex + dy))
        points.append((px_x / BASE_W, px_y / BASE_H))
    return points or None


def wait_and_detect_deploy_points(
    serial: Optional[str],
    first_img: Optional["Image.Image"] = None,
    timeout: float = 2.5,
    stage2_cave: bool = False,
    fallback_pts: Optional[List[Tuple[float, float]]] = None,
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """
    下兵前先偵測地圖 (最多等 timeout 秒讓雲散、紅線出現)。
    回傳 (主要下兵點, 補兵用下兵點)；主要點在紅線最左角外側 42px，補兵點更往外側退至 56px 避免誤觸紅線。
    偵測失敗則退回 fallback_pts (預設 COORDS_RATIO["deploy_points"])。
    """
    img = first_img
    t0 = time.time()
    while True:
        rows = detect_red_line_rows(img, stage2_cave=stage2_cave)
        if len(rows) >= 12:
            main_pts = detect_deploy_points(img, dist_px=42, stage2_cave=stage2_cave)
            retry_pts = detect_deploy_points(img, dist_px=56, stage2_cave=stage2_cave)
            if main_pts:
                rx, ry = min(rows)
                print(f"  -> [地圖偵測] 紅線西角位於 ({rx}, {ry})，下兵點: "
                      + ", ".join(f"({int(x * BASE_W)},{int(y * BASE_H)})" for x, y in main_pts))
                return main_pts, (retry_pts or main_pts)
        if time.time() - t0 >= timeout:
            break
        time.sleep(0.15)
        img = capture_screen_pil(serial)
    print("  -> [地圖偵測] 未偵測到紅線，改用備用下兵點。")
    fallback = fallback_pts or COORDS_RATIO["deploy_points"]
    return fallback, fallback


# =====================================================================
# 點擊與批次下兵指令 (使用單次 input tap 避免誤觸鏡頭拖曳或連點誤開技能)
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


def deploy_troops_batch(
    screen_wh: Tuple[int, int],
    serial: Optional[str],
    deploy_hero: bool = True,
    witch_indices: Optional[List[int]] = None,
    point_offset: int = 0,
    deploy_pts: Optional[List[Tuple[float, float]]] = None,
):
    """
    批次快速部署指定的『未下場英雄』與『未下場暗夜女巫清單』。
    deploy_pts: 由地圖偵測得到的下兵點 (比例座標)；未提供時使用備用點。
    注意：每選一張卡牌只點一次地圖！因為 CoC 放下一隻兵後會自動選取下一張未出戰卡牌，
    若同一張卡點兩次地圖會把下一張卡的兵直接放出去，導致後續點到已出戰卡牌而提前開掉技能。
    """
    if witch_indices is None:
        witch_indices = list(range(6))

    if not deploy_hero and not witch_indices:
        return

    if not deploy_pts:
        deploy_pts = COORDS_RATIO["deploy_points"]
    cmds = []

    # 1. 部署英雄
    if deploy_hero:
        pt_hero = deploy_pts[point_offset % len(deploy_pts)]
        cmds.append(make_tap_cmd(COORDS_RATIO["slot_hero"], screen_wh, jitter_px=5))
        cmds.append("sleep 0.08")
        cmds.append(make_tap_cmd(pt_hero, screen_wh, jitter_px=6))
        cmds.append("sleep 0.08")

    # 2. 部署指定的暗夜女巫格子
    for idx in witch_indices:
        slot_ratio = COORDS_RATIO["slot_witches"][idx]
        pt = deploy_pts[(idx + point_offset) % len(deploy_pts)]

        cmds.append(make_tap_cmd(slot_ratio, screen_wh, jitter_px=5))
        cmds.append("sleep 0.08")
        cmds.append(make_tap_cmd(pt, screen_wh, jitter_px=6))
        cmds.append("sleep 0.08")

    witch_labels = [str(i + 1) for i in witch_indices]
    hero_label = "英雄" if deploy_hero else ""
    witch_str = f"女巫[{','.join(witch_labels)}]" if witch_labels else ""
    join_str = " + " if (hero_label and witch_str) else ""
    print(f"    -> [極速下兵] 部署 {hero_label}{join_str}{witch_str}...")
    run_adb(["shell", "; ".join(cmds)], serial)


def trigger_abilities_smart(
    img: Optional["Image.Image"],
    screen_wh: Tuple[int, int],
    serial: Optional[str],
    stage: int = 1,
):
    """
    智慧觸發技能：只點擊『已上場且技能亮起的女巫』與『存活的英雄』，
    避免浪費時間去點已陣亡或技能冷卻中的卡牌。
    """
    max_witches = 6 if stage == 1 else 8
    cmds = []
    triggered_names = []

    if img is None:
        cmds.append(make_tap_cmd(COORDS_RATIO["slot_hero"], screen_wh, jitter_px=5))
        for i in range(max_witches):
            cmds.append("sleep 0.04")
            cmds.append(make_tap_cmd(COORDS_RATIO["slot_witches"][i], screen_wh, jitter_px=5))
        run_adb(["shell", "; ".join(cmds)], serial)
        return

    if is_hero_deployed_alive(img):
        cmds.append(make_tap_cmd(COORDS_RATIO["slot_hero"], screen_wh, jitter_px=5))
        triggered_names.append("英雄")

    ready_witches = [i for i in range(max_witches) if is_witch_ability_ready(img, i)]
    for i in ready_witches:
        if cmds:
            cmds.append("sleep 0.04")
        cmds.append(make_tap_cmd(COORDS_RATIO["slot_witches"][i], screen_wh, jitter_px=5))
        triggered_names.append(f"女巫{i + 1}")

    # 若精準像素偵測未匹配到任何亮起技能（例如血條/技能條座標微偏），
    # 改為點擊所有「已下場」的格子作為保底（已陣亡或冷卻中的格子點了不會有副作用）
    if not cmds:
        if not is_hero_undeployed(img):
            cmds.append(make_tap_cmd(COORDS_RATIO["slot_hero"], screen_wh, jitter_px=5))
            triggered_names.append("英雄(保底)")
        for i in range(max_witches):
            if not is_witch_undeployed(img, i):
                if cmds:
                    cmds.append("sleep 0.04")
                cmds.append(make_tap_cmd(COORDS_RATIO["slot_witches"][i], screen_wh, jitter_px=5))
                triggered_names.append(f"女巫{i + 1}(保底)")

    if cmds:
        print(f"    -> [智慧技能] 觸發 {', '.join(triggered_names)} 技能")
        run_adb(["shell", "; ".join(cmds)], serial)


# =====================================================================
# 主戰鬥迴圈
# =====================================================================

def run_battle_loop(
    rounds: int,
    max_battle_sec: int = 135,
    serial: Optional[str] = None,
):
    print("===================================================================")
    print("  CoC 夜世界 (Builder Base) 暗夜女巫自動打星腳本 (極速精準優化版)")
    print("===================================================================")
    print("  -> 等待 ADB 裝置連線...", flush=True)
    wait_cmd = ["adb"] + (["-s", serial] if serial else []) + ["wait-for-device"]
    try:
        subprocess.run(wait_cmd, timeout=10)
    except subprocess.TimeoutExpired:
        print("\n[!] 等待 ADB 裝置逾時 (10 秒)！目前 `adb devices` 狀態：")
        print(run_adb(["devices"]) or "(無輸出)")
        print("    請確認手機 USB / 無線偵錯已連線且已授權，必要時可先執行：adb kill-server && adb devices")
        sys.exit(1)

    screen_wh = get_screen_landscape_size(serial)
    print(f"  -> 螢幕橫向解析度 : {screen_wh[0]} x {screen_wh[1]}")
    print(f"  -> 智慧畫面偵測   : {'啟用 (Raw Screencap + 逐格檢查補兵)' if HAS_PIL else '停用'}")
    print(f"  -> 預計執行場數   : {rounds} 場 (按 Ctrl+C 可隨時停止)")
    print("===================================================================")

    # 若啟動腳本時剛好停在上一場的「回營」結算畫面，先自動點回營
    init_img = capture_screen_pil(serial)
    if is_return_home_visible(init_img):
        print("  -> 偵測到目前停在結算畫面，先點擊『回營』回到夜世界村莊...")
        fast_tap(COORDS_RATIO["return_home_btn"], screen_wh, serial=serial, label="回營 (Return Home)")
        time.sleep(2.5)

    for r in range(1, rounds + 1):
        print(f"\n==================== [第 {r} / {rounds} 場對戰] ====================")

        # Step 1: 點擊左下角「戰鬥」，具備視覺確認與自動重試
        print("[Step 1] 點擊左下角『戰鬥』...")
        fast_tap(COORDS_RATIO["attack_btn"], screen_wh, serial=serial, label="戰鬥 (Attack)")

        if HAS_PIL:
            t_wait = time.time()
            while time.time() - t_wait < 5.0:
                time.sleep(0.25)
                img = capture_screen_pil(serial)
                if is_find_now_modal_visible(img):
                    break
                if is_home_screen_visible(img) and time.time() - t_wait >= 1.1:
                    fast_tap(COORDS_RATIO["attack_btn"], screen_wh, serial=serial, label="重試點擊戰鬥")
        else:
            time.sleep(0.9)

        # Step 2: 點擊「立即尋找！」，若彈窗未關閉自動補點
        print("[Step 2] 點擊『立即尋找！』...")
        fast_tap(COORDS_RATIO["find_now_btn"], screen_wh, serial=serial, label="立即尋找！")

        # Step 3: 智慧等待白雲散開進入第一階段戰場 (一偵測到卡牌出現立刻下兵)
        print("[Step 3] 偵測對手配對與載入地圖中...")
        battle_img = None
        if HAS_PIL:
            time.sleep(1.2)
            t_load = time.time()
            while time.time() - t_load < 14.0:
                img = capture_screen_pil(serial)
                if is_find_now_modal_visible(img):
                    fast_tap(COORDS_RATIO["find_now_btn"], screen_wh, serial=serial, label="重試立即尋找！")
                    time.sleep(1.0)
                    continue
                if is_stage1_ready(img):
                    battle_img = img
                    print("  -> [智慧偵測] 戰場載入完成！立刻執行第一階段下兵！")
                    break
                time.sleep(0.2)
        else:
            time.sleep(6.5)

        # 先偵測地圖紅線，再依紅線位置計算下兵點
        stage_pts, retry_pts = (COORDS_RATIO["deploy_points"], COORDS_RATIO["deploy_points"])
        if HAS_PIL:
            stage_pts, retry_pts = wait_and_detect_deploy_points(serial, battle_img)

        # 第一階段全下 (開戰預設英雄格可能呈預選突起狀態，直接強制下英雄 + 1~6 號女巫)
        deploy_troops_batch(
            screen_wh,
            serial,
            deploy_hero=True,
            witch_indices=list(range(6)),
            point_offset=0,
            deploy_pts=stage_pts,
        )
        last_deploy_ts = time.time()

        # Step 4: 高頻智慧監控戰鬥進度 (自動檢查漏放兵補下 + 自動偵測二階段 + 自動開技能 + 偵測回營)
        start_ts = time.time()
        stage = 1
        stage1_all_deployed_seen = False
        last_ability_ts = time.time()
        retry_count = 0

        print("[Step 4] 高頻監控戰鬥進度中 (自動補放漏兵、偵測二階段、智慧開技能、秒回營)...")
        while time.time() - start_ts < max_battle_sec:
            time.sleep(0.35)
            img = capture_screen_pil(serial)

            # A. 檢查是否已出現「回營」結算畫面
            if is_return_home_visible(img):
                print("  -> [智慧偵測] 戰鬥結束！偵測到『回營』按鈕！")
                break

            # B. 檢查是否進入第二階段：
            #    1) 第 7 或 8 格增援女巫出現未下場狀態；或
            #    2) 一階段已全數下場後，經過 10 秒以上又出現未下場的英雄/女巫 (一階段存活兵重新回到牌組)
            stage2_by_reappear = (
                stage1_all_deployed_seen
                and img is not None
                and (time.time() - start_ts >= 10.0)
                and (is_hero_undeployed(img) or len(get_undeployed_witches(img, 8)) >= 1)
            )
            if stage == 1 and (is_stage2_ready(img) or stage2_by_reappear):
                print("  -> [智慧偵測] 第一階段 100% 三星！已進入第二階段，等待地圖載入並偵測紅線...")
                stage = 2
                retry_count = 0

                # 稍等過場動畫並偵測第二階段紅線 (支援洞穴暗色地面；若偵測失敗則退回多點備用座標)
                time.sleep(0.6)
                stage2_img = capture_screen_pil(serial) or img
                stage_pts, retry_pts = wait_and_detect_deploy_points(
                    serial,
                    stage2_img,
                    timeout=2.5,
                    stage2_cave=True,
                    fallback_pts=COORDS_RATIO["stage2_deploy_points"],
                )
                if retry_pts == stage_pts:
                    retry_pts = COORDS_RATIO["stage2_deploy_points"]

                # 重新取得最新畫面，確保一階段存活且回到牌組的女巫與第 7、8 格增援全數下場
                latest_img = capture_screen_pil(serial) or stage2_img
                stage2_undep_witches = get_undeployed_witches(latest_img, 8)
                if not stage2_undep_witches:
                    stage2_undep_witches = [6, 7]
                deploy_hero_stage2 = is_hero_undeployed(latest_img)

                deploy_troops_batch(
                    screen_wh,
                    serial,
                    deploy_hero=deploy_hero_stage2,
                    witch_indices=stage2_undep_witches,
                    point_offset=0,
                    deploy_pts=stage_pts,
                )
                last_deploy_ts = time.time()
                last_ability_ts = time.time()
                continue

            # C. 逐格檢查是否有「沒放下去」的英雄或女巫，若有換位置補放！
            #    下兵後至少等 1.2 秒讓卡牌動畫上移完成，避免誤把已下場卡牌當成未下場而提前點開技能。
            if img is not None and time.time() - last_deploy_ts >= 1.2:
                max_slots = 6 if stage == 1 else 8
                hero_undep = is_hero_undeployed(img)
                witches_undep = get_undeployed_witches(img, max_slots)
                if stage == 1 and not hero_undep and not witches_undep:
                    stage1_all_deployed_seen = True
                if hero_undep or witches_undep:
                    retry_count += 1
                    print(
                        f"  -> [自動補兵] 偵測到尚未下場單位 "
                        f"({'英雄 ' if hero_undep else ''}"
                        f"{'女巫' + str([i + 1 for i in witches_undep]) if witches_undep else ''})，換位置補放！"
                    )
                    if retry_count % 3 == 1:
                        active_pts = retry_pts
                    elif retry_count % 3 == 2:
                        active_pts = stage_pts
                    else:
                        active_pts = (
                            COORDS_RATIO["stage2_deploy_points"]
                            if stage == 2
                            else COORDS_RATIO["deploy_points"]
                        )
                    deploy_troops_batch(
                        screen_wh,
                        serial,
                        deploy_hero=hero_undep,
                        witch_indices=witches_undep,
                        point_offset=retry_count,
                        deploy_pts=active_pts,
                    )
                    last_deploy_ts = time.time()
                    if time.time() - last_ability_ts >= 4.5:
                        trigger_abilities_smart(img, screen_wh, serial, stage=stage)
                        last_ability_ts = time.time()
                    continue

            # D. 無 PIL 時的保底二階段下兵
            elapsed = time.time() - start_ts
            if not HAS_PIL and stage == 1 and elapsed >= 38:
                stage = 2
                deploy_troops_batch(screen_wh, serial, deploy_hero=True, witch_indices=list(range(8)))
                last_ability_ts = time.time()
                continue

            # E. 每 4.5 秒智慧觸發一次就緒的英雄與女巫技能
            if time.time() - last_ability_ts >= 4.5:
                trigger_abilities_smart(img, screen_wh, serial, stage=stage)
                last_ability_ts = time.time()

        # Step 5: 點擊「回營」回到村莊，並用視覺偵測回村完成 (若有星數獎勵視窗一併關閉)
        print("[Step 5] 點擊『回營 (Return Home)』返回夜世界村莊...")
        fast_tap(COORDS_RATIO["return_home_btn"], screen_wh, serial=serial, label="回營 (Return Home)")

        if HAS_PIL:
            time.sleep(1.5)
            t_home = time.time()
            bonus_clicked = False
            while time.time() - t_home < 6.0:
                img = capture_screen_pil(serial)
                if is_home_screen_visible(img):
                    break
                if is_return_home_visible(img):
                    fast_tap(COORDS_RATIO["return_home_btn"], screen_wh, serial=serial)
                elif not bonus_clicked and time.time() - t_home >= 2.3:
                    fast_tap(COORDS_RATIO["star_bonus_ok_btn"], screen_wh, serial=serial, label="關閉獎勵彈窗(若有)")
                    bonus_clicked = True
                time.sleep(0.25)
        else:
            time.sleep(3.8)
            fast_tap(COORDS_RATIO["star_bonus_ok_btn"], screen_wh, serial=serial)
            time.sleep(1.2)

    print("\n===================================================================")
    print(f"  已完成全部 {rounds} 場夜世界暗夜女巫自動打星！")
    print("===================================================================")


def main():
    parser = argparse.ArgumentParser(description="CoC 夜世界暗夜女巫自動打星腳本 (極速精準優化版)")
    parser.add_argument("-n", "--rounds", type=int, default=15, help="要連續執行的對戰場數 (預設: 15)")
    parser.add_argument("-s", "--serial", type=str, default=None, help="指定 ADB 裝置序號")
    parser.add_argument("--max-sec", type=int, default=135, help="單場最長等待秒數 (預設: 135 秒)")
    args = parser.parse_args()

    try:
        run_battle_loop(
            rounds=args.rounds,
            max_battle_sec=args.max_sec,
            serial=args.serial,
        )
    except KeyboardInterrupt:
        print("\n[!] 已收到 Ctrl+C，腳本安全停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
