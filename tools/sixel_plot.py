#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sixel_plot.py —— 把序列数据渲染成 Sixel 二维柱状图（纯标准库，零第三方依赖）。

Sixel 是一种「按像素寻址」的图形协议：图像是一张固定宽高的栅格，
每个 band 覆盖 6 像素行。所以本模块画的是**竖向柱状图**——
时间沿 X 轴展开、值沿 Y 轴上升，而不是终端字符图那种「一行一根横条」。

本模块的所有函数都是纯函数：读参数、返回值，不读全局变量、不写 stdout。
渲染结果一律先经 verify_chart() 解码回像素自检，通过了才交给调用方输出——
结构性检查（有没有 DCS、重复指令在不在、调色板全不全）就算全绿，
也照样能配出一张一个像素都没画的空白图，唯一能抓住那类错误的就是像素语义。

用法概览：
    payload, layout, ok, problems, stats = render_chart([list_a, list_b], max_value)
    if not ok:
        print(problems, file=sys.stderr)   # 调用方自行回退到字符图
    else:
        sys.stdout.write(payload)
"""

import re
from collections import namedtuple

# ---------- 画布几何 ----------

# VT340 的 sixel 栅格宽度上限。Windows Terminal 会把 Sixel 图缩放到
# 「字符单元格 × 单元格像素宽」的尺寸，800px 恰好是 80 列，不赌更大的栅格。
CANVAS_WIDTH = 800

# 一个 sixel band 覆盖 6 像素行；高度必须是 6 的整数倍。
BAND_HEIGHT = 6

# 默认图高 240px = 40 个 band = 40 个终端行。
DEFAULT_CHART_HEIGHT = 240

# VT340 标准单元格宽度 10px，用来把像素列换算成终端字符列（供坐标轴刻度定位）。
CELL_WIDTH_PX = 10

MIN_BAR_WIDTH = 1     # 柱子最窄 1px，再窄就只剩「有没有」的区别了
BASELINE_BANDS = 1    # 底部基线占几个 band
GRID_BANDS = 4        # 横向网格线条数（把绘图区等分）

# ---------- Y 轴刻度（像素点阵字体） ----------

# Windows Terminal 把 Sixel 图缩放到「字符单元格 × 单元格像素宽」的尺寸，
# 800px 宽的图就是 80 列。所以图内想写出人能读的数字，最小字号是一个
# 字符格 10×20px——比这更小的字形缩放后会糊成一团。
#
# 竖直方向有个绕不开的硬限制：Sixel 的一个 band 覆盖 6 像素行，
# 同一列在一个 band 内只能有一种颜色，所以**字形必须是 6 的整数倍高**，
# 每个点恰好占满一个 band。横向倒是逐像素的，点宽可以自由取。
# 5 个 band（30px）× 每点 3px（15px 宽）是能认出 0-9 的最小字号。
GLYPH_DOT = 3                          # 横向每个点占几个像素
GLYPH_COLS = 5                         # 字形点阵列数（横向）
GLYPH_ROWS = 5                         # 字形点阵行数（竖向，1 行 = 1 个 band）
GLYPH_WIDTH = GLYPH_COLS * GLYPH_DOT   # 15px
GLYPH_HEIGHT = GLYPH_ROWS * BAND_HEIGHT  # 30px
GLYPH_ADVANCE = GLYPH_WIDTH + 3        # 18px：15px 字形 + 3px 字间距

# 刻度分档是固定的（0/25%/50%/75%/100%），但刻度值恒等于当图最大值的对应比例，
# 任何时候都由 max_value 现算，绝不硬编码具体数字。
TICK_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)

# ---------- 调色板 ----------

PALETTE = [
    (24, 24, 28),     # 0 背景
    (72, 76, 84),     # 1 轨道 / 网格线 / 基线
    (0, 200, 220),    # 2 A 侧青色
    (224, 64, 200),   # 3 B 侧品红
    (250, 206, 70),   # 4 缺测标记黄
    (176, 182, 192),  # 5 Y 轴刻度文字（比轨道色亮，免得读成柱子）
]
BACKGROUND = 0
TRACK = 1
MISSING = 4
AXIS_TEXT = 5
GRID_COLOR = TRACK
# 并列系列的配色，按系列下标依次取用；单系列图只会用到第一个。
SERIES_COLORS = (2, 3)


# ---------- Sixel 编码 ----------

def solid_band(color, width):
    """
    一个满高（6 像素）band 的实心色条，用 R（!）重复指令压缩。

    字符千万别写反：Sixel 数据位的含义是 ord(ch) - 0x3F，所以
    0x3F('?') 表示六像素全空、0x7E('~') 才是六像素全实。
    写成 '?' 会得到一张完全空白、却没有任何报错的图。
    """
    if width <= 0:
        return f'#{color}'
    return f'#{color}' + (f'!{width}~' if width > 4 else '~' * width)


def bands_from_grid(grid, width):
    """
    把逐 band 的像素颜色矩阵编码成 Sixel band 串（每个 band 一个字符串）。

    同一行内相邻的同色像素合并成一条 solid_band，既省字节也让自检的
    覆盖率判定有意义——整张图铺满背景色，因此覆盖率恒为 100%。
    """
    bands = []
    for row in grid:
        parts = []
        x = 0
        while x < width:
            color = row[x]
            run = 1
            while x + run < width and row[x + run] == color:
                run += 1
            parts.append(solid_band(color, run))
            x += run
        bands.append(''.join(parts))
    return bands


def encode_sixel(bands, width, height):
    """
    加上调色板与 DCS/ST 包裹，输出完整 Sixel 串。

    DCS 参数写成 0;1;0（P1=0 选用 sixel 设备、P2=1 不透明背景、P3=0），
    与 libsixel / chafa 的输出一致；前后隐藏与恢复光标，免得光标停在图上。
    """
    parts = ['\x1b[?25l', f'\x1bP0;1;0q"1;1;{width};{height}']
    for i, (r, g, b) in enumerate(PALETTE):
        parts.append(f'#{i};2;{r};{g};{b}')
    parts.append('-'.join(bands))
    parts.append('\x1b\\')
    parts.append('\x1b[?25h')
    return ''.join(parts)


# ---------- Y 轴刻度 ----------

# 5×5 点阵数字。每个 '1' 横向放大成 GLYPH_DOT 个像素、竖向占满一个 band。
GLYPH_3x5 = {
    '0': ('11111', '10001', '10001', '10001', '11111'),
    '1': ('00100', '01100', '00100', '00100', '11111'),
    '2': ('11111', '00001', '11111', '10000', '11111'),
    '3': ('11111', '00001', '11111', '00001', '11111'),
    '4': ('10001', '10001', '11111', '00001', '00001'),
    '5': ('11111', '10000', '11111', '00001', '11111'),
    '6': ('11111', '10000', '11111', '10001', '11111'),
    '7': ('11111', '00001', '00010', '00100', '00100'),
    '8': ('11111', '10001', '11111', '10001', '11111'),
    '9': ('11111', '10001', '11111', '00001', '11111'),
}


def y_axis_ticks(max_value):
    """
    自下而上的 5 档刻度值，恒为 max_value 的固定比例。

    分档是固定的（0/25%/50%/75%/100%），但刻度值永远由当图最大值现算：
    max=393 得到 [0, 98, 196, 295, 393]，max=20 得到 [0, 5, 10, 15, 20]。
    没有任何硬编码刻度数值。
    """
    if max_value is None or max_value <= 0:
        return []
    return [int(round(max_value * frac)) for frac in TICK_FRACTIONS]


def y_axis_width_px(max_value, pixel_ticks=True):
    """
    左侧刻度栏宽度：刚好放得下最大值那串数字。

    宽度随数据自适应——两位数的图只让出 36px，把省下的还给柱子；
    栏宽是逐像素而不是逐字符，绘图区自然就等于 CANVAS_WIDTH 减去它。

    Args:
        pixel_ticks (bool): True 用像素点阵字（每字符位 18px）；
            False 用终端真字体（每字符位恰好 1 个字符格 CELL_WIDTH_PX），
            于是 digits 位数正好等于 digits 个字符列，方便在图外
            用光标定位把刻度值写回这一栏。
    """
    digits = max(1, len(str(int(max_value))))
    if pixel_ticks:
        return digits * GLYPH_ADVANCE
    return digits * CELL_WIDTH_PX


def gridline_bands(plot_bands):
    """
    4 条网格线自下而上的 band 行号（0.25/0.50/0.75/1.00 高度处），不含 0 基线。

    画网格线和摆刻度文字都必须调这里——两处各算一遍的话，
    迟早会出现「线画在 197、标签写 295」这种对不上的情况。
    """
    return [plot_bands - (plot_bands * k // (GRID_BANDS + 1))
            for k in range(1, GRID_BANDS + 1)]


def tick_label_band(row, n_bands):
    """刻度文字顶边所在的 band 行号：中心对齐到该刻度，再夹在画布内。"""
    band = row - GLYPH_ROWS // 2
    return max(0, min(band, n_bands - GLYPH_ROWS))


def y_tick_rows(max_value, height):
    """
    各档刻度对应的 (刻度值, 所在 band 行号)，自下而上排列。

    band 行号就是网格线所在的行——刻度文字与网格线必须同处一行，
    否则会出现「标签指着 197、线画在 295」这种对不上的图。

    刻度值取整后可能撞在一起（max=1 时 25% 和 50% 都是 0），
    撞车的只保留先出现的那一档，免得同一行叠两个数字。
    """
    if max_value is None or max_value <= 0:
        return []
    n_bands = height // BAND_HEIGHT
    plot_bands = n_bands - BASELINE_BANDS
    rows = []
    previous = None
    for frac, row in zip(TICK_FRACTIONS,
                         [plot_bands] + gridline_bands(plot_bands)):
        value = int(round(max_value * frac))
        if value == previous:
            continue
        previous = value
        rows.append((value, row))
    return rows


def draw_digit(grid, x, band, digit, color=AXIS_TEXT):
    """
    把一个数字的点阵烧进逐 band 的像素网格。非数字字符静默跳过。

    竖向每个点占满一个 band——这是 Sixel 的硬限制，同一列在一个 band 内
    只能有一种颜色，字形高度必须对齐到 band 边界，否则会被抹平变形。
    """
    pattern = GLYPH_3x5.get(digit)
    if pattern is None:
        return
    n_bands = len(grid)
    for r, row in enumerate(pattern):
        target_band = band + r
        if not (0 <= target_band < n_bands):
            continue
        target = grid[target_band]
        for c, ch in enumerate(row):
            if ch != '1':
                continue
            left = x + c * GLYPH_DOT
            for dx in range(GLYPH_DOT):
                px = left + dx
                if 0 <= px < len(target):
                    target[px] = color


def draw_tick_labels(grid, max_value, plot_x0):
    """
    在左侧刻度栏里写出各档刻度值，右对齐紧贴绘图区左边线。

    各档落在哪一行由 y_tick_rows() 统一给出（与网格线同处一行），
    这里只负责把点阵字形烧进去；取整撞车的档已在上游去重。
    """
    n_bands = len(grid)
    for value, row in y_tick_rows(max_value, n_bands * BAND_HEIGHT):
        text = str(value)
        if len(text) * GLYPH_ADVANCE > plot_x0:
            continue      # 理论上不会发生：刻度值恒 <= max_value
        band = tick_label_band(row, n_bands)
        left = plot_x0 - len(text) * GLYPH_ADVANCE
        for i, digit in enumerate(text):
            draw_digit(grid, left + i * GLYPH_ADVANCE, band, digit)


# ---------- 版面分配 ----------

class Layout(namedtuple('Layout',
                         'bars bar_width inner_gap stride overflow used_width')):
    """layout_groups 的返回值。"""

    __slots__ = ()

    def columns(self, width=CANVAS_WIDTH):
        """每个数据点中心所在的终端字符列，供坐标轴刻度文字对齐。"""
        if not self.bars:
            return []
        return [canvas_columns(width)[min(x + w - 1, width - 1)]
                for x, w in self.bars]


def canvas_columns(width=CANVAS_WIDTH):
    """像素列数 → 终端字符列数（800px → 80 列）。"""
    return max(1, width // CELL_WIDTH_PX)


def column_of_point(index, n_points, width=CANVAS_WIDTH):
    """第 index 个数据点在画布上大约落在第几个终端字符列。"""
    if n_points <= 0:
        return 0
    slot = width / n_points
    px = min(width - 1, max(0, int(index * slot + slot / 2)))
    return px // CELL_WIDTH_PX


def layout_groups(n_points, n_series, width=CANVAS_WIDTH, x0=0):
    """
    把画布宽度切给 n_points 个时间点、每个时间点 n_series 根并列柱。

    返回的 bars 是展平后的 [(x 起点, 柱宽), ...]，顺序为「组内系列优先」：
    第 k 根柱对应 series = k % n_series、point = k // n_series。
    x 已加上偏移 x0（Y 轴刻度栏宽度），因此是画布上的绝对像素列。

    宽度不够时的降级顺序：先去掉组内间隙 → 再把柱宽压到 1px。
    连 1px 都放不下（n_points * n_series > width）就连续绘制、
    后面的点覆盖前面的点，并用 overflow 标记如实告知，绝不悄悄丢数据。

    Args:
        n_points (int): 时间点数量。
        n_series (int): 每个时间点的并列系列数。
        width (int): 绘图区像素宽度（不含左侧刻度栏）。
        x0 (int): 绘图区左边界在画布上的像素列。

    Returns:
        Layout: bars / bar_width / inner_gap / stride / overflow / used_width
    """
    if n_points <= 0 or n_series <= 0:
        raise ValueError('n_points 与 n_series 必须为正')

    if n_points * n_series > width:
        bars = [(x0 + i * n_series + s, MIN_BAR_WIDTH)
                for i in range(n_points) for s in range(n_series)
                if i * n_series + s < width]
        return Layout(bars, MIN_BAR_WIDTH, 0, 1, True, len(bars))

    slot = width / n_points
    inner_gap = 1 if (n_series > 1 and slot >= 2 * n_series + 1) else 0
    bar_width = max(MIN_BAR_WIDTH,
                    int((slot - inner_gap * (n_series - 1)) // n_series))

    # 柱宽被 MIN_BAR_WIDTH 顶大时可能挤到与下一组重叠，这时退回无间隙摆法
    if bar_width * n_series + inner_gap * (n_series - 1) > slot:
        inner_gap = 0
        bar_width = max(MIN_BAR_WIDTH, int(slot // n_series))

    bars = []
    for i in range(n_points):
        x_start = x0 + int(i * slot)
        for s in range(n_series):
            bars.append((x_start + s * (bar_width + inner_gap), bar_width))

    stride = bar_width * n_series + inner_gap * (n_series - 1)
    used = bars[-1][0] + bars[-1][1] - x0 if bars else 0
    return Layout(bars, bar_width, inner_gap, stride, False, used)


# ---------- 柱状图绘制 ----------

def bar_fill_bands(value, max_value, plot_bands):
    """
    某个值应从底部填满几个 band；缺测返回 None，值为 0 返回 0。

    Sixel 的一个 band 覆盖 6 像素行，绘制只能按 band 整体上色，
    因此柱高天然量化到 6 像素。与字符图一致：任何非零值至少占 1 个 band
    （对应字符图的「至少 1/8 格」），避免极小值完全看不见。
    """
    if value is None:
        return None
    if max_value is None or max_value <= 0:
        return 0
    ratio = max(0.0, min(1.0, value / max_value))
    bands = int(round(ratio * plot_bands))
    if ratio > 0:
        bands = max(1, bands)
    return max(0, min(plot_bands, bands))


def build_grid(values_by_series, max_value, layout, width=CANVAS_WIDTH,
               height=DEFAULT_CHART_HEIGHT, plot_x0=0, pixel_ticks=True):
    """
    画出逐 band 的像素颜色矩阵，返回 (grid, plot_bands)。

    grid[row][x] 是颜色索引；行号自上而下，x 自左而右。
    整张图先铺满背景色，因此编码出来的覆盖率恒为 100%。

    Args:
        plot_x0 (int): 绘图区左边界（像素列）。其左侧是 Y 轴刻度栏：
            网格线与基线只画在绘图区内，刻度文字另行烧录，两者互不覆盖。
        pixel_ticks (bool): True 把 Y 轴刻度点阵字烧进刻度栏；
            False 留白，交给调用方用终端真字体覆盖书写（见 render_chart）。
    """
    if height % BAND_HEIGHT:
        raise ValueError(f'height 必须是 {BAND_HEIGHT} 的整数倍')
    n_bands = height // BAND_HEIGHT
    if n_bands <= BASELINE_BANDS + 1:
        raise ValueError(f'height 太小，至少需要 {(BASELINE_BANDS + 2) * BAND_HEIGHT}px')

    grid = [[BACKGROUND] * width for _ in range(n_bands)]
    plot_bands = n_bands - BASELINE_BANDS
    base_row = plot_bands           # 基线所在的 band 行号

    # 横向网格线：把绘图区等分成 GRID_BANDS + 1 份
    for row in gridline_bands(plot_bands):
        if 0 <= row < base_row:
            for x in range(plot_x0, width):
                grid[row][x] = GRID_COLOR

    # 基线
    for x in range(plot_x0, width):
        grid[base_row][x] = GRID_COLOR

    n_series = len(values_by_series)

    for k, (x, w) in enumerate(layout.bars):
        s = k % n_series
        i = k // n_series
        if s >= n_series or i >= len(values_by_series[s]):
            continue
        value = values_by_series[s][i]
        x_end = min(x + w, width)

        if value is None:
            # 缺测：Sixel 画不了文字，空白又会被误读成 0，
            # 所以在基线上方贴一小段强调色短横作为「这里没有数据」的标记。
            row = base_row - 1
            if row >= 0:
                for xx in range(x, x_end):
                    grid[row][xx] = MISSING
            continue

        # 先铺满轨道（代表这把标尺的满量程），再从底部填出实际高度
        fill = bar_fill_bands(value, max_value, plot_bands)
        top_row = plot_bands - fill
        for row in range(0, top_row):
            for xx in range(x, x_end):
                grid[row][xx] = TRACK
        color = SERIES_COLORS[s % len(SERIES_COLORS)]
        for row in range(top_row, plot_bands):
            for xx in range(x, x_end):
                grid[row][xx] = color

    # 刻度文字最后烧录：它住在绘图区之外的刻度栏里，与柱子不会抢位置，
    # 但放在最后可以保证不会被前面任何一步覆盖掉。
    if plot_x0 > 0 and pixel_ticks:
        draw_tick_labels(grid, max_value, plot_x0)

    return grid, plot_bands


# ---------- 自检：把输出解码回像素，验证语义 ----------

def decode_sixel(text):
    """
    最小 Sixel 解码器，仅供自检与测试使用。

    返回 (dcs_params, raster_attrs, width, height, pixels)，pixels 是
    {(x, y): (r, g, b)}。width / height 取实际着色像素的外接范围。
    """
    m = re.search(r'\x1bP([0-9;]*)q(.*?)(?:\x1b\\|\x9c)', text, re.S)
    if not m:
        raise ValueError('没有找到 DCS ... q ... ST 结构')
    params, body = m.group(1), m.group(2)

    raster = ''
    m2 = re.match(r'"([0-9;]*)', body)
    if m2:
        raster = m2.group(1)
        body = body[m2.end():]

    palette, current = {}, None
    x = y = 0
    pixels = {}
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        m3 = re.match(r'#(\d+);(\d+);(\d+);(\d+);(\d+)', body[i:])
        if m3:  # 定义调色板项 #idx;2;r;g;b
            g = m3.groups()
            palette[int(g[0])] = tuple(int(v) for v in g[2:5])
            i += m3.end()
            continue
        m4 = re.match(r'#(\d+)', body[i:])
        if m4:  # 选用调色板项
            current = int(m4.group(1))
            i += m4.end()
            continue
        if ch == '!':  # R 重复指令 !<次数><字符>
            m5 = re.match(r'!(\d+)(.)', body[i:], re.S)
            if not m5:
                i += 1
                continue
            bits = ord(m5.group(2)) - 0x3F
            for _ in range(int(m5.group(1))):
                for bit in range(6):
                    if bits & (1 << bit):
                        pixels[(x, y + bit)] = palette.get(current)
                x += 1
            i += m5.end()
            continue
        if ch == '$':  # 回车到 band 行首
            x = 0
            i += 1
            continue
        if ch == '-':  # 换到下一个 band
            x = 0
            y += BAND_HEIGHT
            i += 1
            continue
        if ch == '"':  # 栅格属性，已在上面处理
            m6 = re.match(r'"[0-9;]*', body[i:])
            i += m6.end() if m6 else 1
            continue
        if 0x3F <= ord(ch) <= 0x7E:  # 数据字符
            bits = ord(ch) - 0x3F
            for bit in range(6):
                if bits & (1 << bit):
                    pixels[(x, y + bit)] = palette.get(current)
            x += 1
            i += 1
            continue
        i += 1

    width = max((p[0] for p in pixels), default=-1) + 1
    height = max((p[1] for p in pixels), default=-1) + 1
    return params, raster, width, height, pixels


def verify_chart(values_by_series, max_value, layout, grid, bands, payload,
                 width=CANVAS_WIDTH, height=DEFAULT_CHART_HEIGHT, plot_x0=0):
    """
    把生成的流解码回来，逐项断言像素语义是否符合预期。

    校验项：
      1. 解码尺寸 == 声明的栅格尺寸（多画了说明漏了 $ 让游标回退）
      2. 覆盖率 == 100%（覆盖率为 0 就是把全空字符 "?" 当成了全实字符）
      3. 逐柱核对柱底 / 柱顶 / 柱顶之上的颜色（抓布局算错、比例算错）
      4. 缺测位置确实画了标记色，且没有画出柱子
      5. 并列系列之间颜色不同，否则图上分不出谁是谁
      6. Y 轴每一档刻度都在左侧刻度栏里留下了非背景像素

    Returns:
        tuple: (是否通过, 问题描述列表, 统计信息字典)
    """
    params, raster, got_w, got_h, pixels = decode_sixel(payload)
    plot_bands = (height // BAND_HEIGHT) - BASELINE_BANDS
    n_series = len(values_by_series)

    problems = []
    # 覆盖率先查：整张图一个像素都没画出来时，宽度检查会跟着一起炸，
    # 那时报「栅格宽度不符」只会把人引到错误的方向上去。
    coverage = len(pixels) / (width * height) if width and height else 0
    if coverage < 0.999:
        problems.append(f'覆盖率仅 {coverage:.1%}，应为 100%'
                       f'（多半是把全空字符 "?" 当成了全实字符）')
    if got_w != width:
        problems.append(f'解码宽度 {got_w} != 声明宽度 {width}'
                        f'（要么画超出了声明栅格，要么整行没画满）')
    if got_h != height:
        problems.append(f'解码高度 {got_h} != 声明高度 {height}'
                        f'（多半是 band 数与 raster 属性对不上）')

    def color_at_band(x, row):
        """取某个 band 行的中间那 6 个像素行中的第 3 行的颜色。"""
        return pixels.get((x, row * BAND_HEIGHT + 3))

    for k, (x, w) in enumerate(layout.bars):
        s = k % n_series
        i = k // n_series
        if s >= n_series or i >= len(values_by_series[s]):
            continue
        value = values_by_series[s][i]
        mid = x + w // 2
        color = SERIES_COLORS[s % len(SERIES_COLORS)]
        where = f'第 {i} 点系列 {s}'

        if value is None:
            if color_at_band(mid, plot_bands - 1) != PALETTE[MISSING]:
                problems.append(f'{where} 缺测，基线上方应为标记色 '
                                f'{PALETTE[MISSING]}，实际 {color_at_band(mid, plot_bands - 1)}')
            continue

        fill = bar_fill_bands(value, max_value, plot_bands)
        if fill >= 1:
            if color_at_band(mid, plot_bands - 1) != PALETTE[color]:
                problems.append(f'{where} 柱底应为颜色 {color}，'
                                f'实际 {color_at_band(mid, plot_bands - 1)}')
            top_row = plot_bands - fill
            if color_at_band(mid, top_row) != PALETTE[color]:
                problems.append(f'{where} 柱顶(band {top_row}) 应为颜色 {color}，'
                                f'实际 {color_at_band(mid, top_row)}')
            if top_row - 1 >= 0:
                above = color_at_band(mid, top_row - 1)
                if above != PALETTE[TRACK]:
                    problems.append(f'{where} 柱顶之上应为轨道色 {PALETTE[TRACK]}，'
                                    f'实际 {above}')
        else:  # 值为 0：空柱，整根都是轨道色
            if color_at_band(mid, plot_bands - 1) != PALETTE[TRACK]:
                problems.append(f'{where} 值为 0，应为空柱（轨道色 {PALETTE[TRACK]}），'
                                f'实际 {color_at_band(mid, plot_bands - 1)}')

    if n_series >= 2:
        c0 = SERIES_COLORS[0 % len(SERIES_COLORS)]
        c1 = SERIES_COLORS[1 % len(SERIES_COLORS)]
        if c0 == c1:
            problems.append(f'并列系列共用颜色 {c0}，A/B 两侧无法区分')

    # Y 轴刻度：每一档都该在刻度栏里留下非背景像素。
    # 按整个字形高度（GLYPH_ROWS 个 band）扫描，而不是只探一个 band——
    # 只采样单行的话，字形一旦整体偏移一格就会漏判。
    if plot_x0 > 0:
        background = PALETTE[BACKGROUND]
        n_bands = height // BAND_HEIGHT
        for value, row in y_tick_rows(max_value, height):
            band = tick_label_band(row, n_bands)
            inked = any(
                pixels.get((x, y)) not in (None, background)
                for b in range(band, band + GLYPH_ROWS)
                for y in range(b * BAND_HEIGHT, (b + 1) * BAND_HEIGHT)
                for x in range(plot_x0)
            )
            if not inked:
                problems.append(
                    f'Y 轴刻度 {value} 在左侧刻度栏里找不到像素'
                    f'（band {band}~{band + GLYPH_ROWS - 1}）')

    stats = {
        'dcs_params': params,
        'raster_attrs': raster,
        'decoded_size': (got_w, got_h),
        'declared_size': (width, height),
        'coverage': coverage,
        'colored_pixels': len(pixels),
        'bar_width': layout.bar_width,
        'overflow': layout.overflow,
        'plot_x0': plot_x0,
        'plot_width': width - plot_x0,
        'y_ticks': y_axis_ticks(max_value),
    }
    return (not problems), problems, stats


# ---------- 对外主入口 ----------

def render_chart(values_by_series, max_value, width=CANVAS_WIDTH,
                 height=DEFAULT_CHART_HEIGHT, pixel_ticks=True):
    """
    渲染一张竖向柱状图，左侧带 Y 轴刻度栏。

    Args:
        values_by_series (list): 每个系列一个数值列表，长度应一致；
            元素为 None 表示该点缺测。
        max_value (int): 标尺上限（各系列共用一把标尺）。
        width (int): 画布像素宽度。
        height (int): 画布像素高度，必须是 6 的整数倍。
        pixel_ticks (bool): True 在刻度栏里画像素点阵数字（默认，
            与旧行为完全一致）；False 留出空栏并把图像整体右移
            plot_x0 个像素后再裁剪掉左栏——图像从此只覆盖
            「gutter_cols 列及之后」的字符格，刻度栏那几列从未被
            图像占据，调用方才可以安全地用终端字体在上面写文字
            （多数终端在 sixel 图像区域内写字符会把整张图擦掉）。

    Returns:
        tuple: (payload, layout, ok, problems, stats)
        payload 为空串表示没画成（problems 里说明原因）。
        调用方拿到 ok 为 False 时应当回退到字符图，而不是把坏图发出去。
        stats 里的 plot_x0 / plot_width 恒为「逻辑绘图区」坐标（即含
        刻度栏偏移、未裁剪），与 pixel_ticks 无关；gutter_cols 是
        图像左边界所在的终端字符列。
        layout.bars 里的 x 同样是含 plot_x0 偏移的逻辑坐标，
        pixel_ticks=False 时实际像素列 = x - plot_x0。
    """
    if not values_by_series or not values_by_series[0]:
        return '', None, False, ['没有可绘制的数据点'], {}

    n_points = min(len(s) for s in values_by_series)
    values_by_series = [list(s)[:n_points] for s in values_by_series]

    known = [v for s in values_by_series for v in s if v is not None]
    if not known:
        return '', None, False, ['所有值均为缺测'], {}
    if max_value is None or max_value <= 0:
        return '', None, False, [f'标尺最大值 {max_value} 无法作为标尺'], {}

    # 刻度栏宽度按最大值那串数字的位数自适应，剩下的一律给柱子
    plot_x0 = y_axis_width_px(max_value, pixel_ticks)
    plot_width = width - plot_x0
    if plot_width <= 0:
        return '', None, False, [f'Y 轴刻度栏（{plot_x0}px）占满了整块画布'], {}

    layout = layout_groups(n_points, len(values_by_series), plot_width, x0=plot_x0)
    grid, _ = build_grid(values_by_series, max_value, layout, width, height,
                         plot_x0=plot_x0, pixel_ticks=pixel_ticks)

    if pixel_ticks:
        image_width, image_x0 = width, plot_x0
        image_layout = layout
    else:
        # 裁掉左栏：图像只承载绘图区，刻度栏留给终端真字体
        image_width, image_x0 = plot_width, 0
        image_layout = layout._replace(
            bars=[(x - plot_x0, w) for x, w in layout.bars])
        grid = [row[plot_x0:] for row in grid]

    bands = bands_from_grid(grid, image_width)
    payload = encode_sixel(bands, image_width, height)
    ok, problems, stats = verify_chart(values_by_series, max_value,
                                       image_layout, grid, bands, payload,
                                       image_width, height, plot_x0=image_x0)
    if not ok:
        return '', layout, False, problems, stats

    # 对外统计恒用逻辑坐标：调用方据此算刻度栏列数与坐标轴落点
    stats.update(plot_x0=plot_x0, plot_width=plot_width,
                 pixel_ticks=pixel_ticks,
                 gutter_cols=plot_x0 // CELL_WIDTH_PX)
    return payload, layout, True, [], stats