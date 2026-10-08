import gc
import json
import os
import select
import socket
import sys
import time

import machine
import micropython
import network

from camera import Camera, FrameSize, GrabMode, PixelFormat

try:
    import jpeg
    HAVE_JPEG = True
except ImportError:
    HAVE_JPEG = False

# ===========================================================================
#  Settings
# ===========================================================================
BAUD        = 230400    # CH340 link speed; usb_viewer.html must use the same value
SAFE_S      = 4         # seconds at 115200 after boot: Ctrl-C in this window keeps the normal REPL
USB_QUALITY = 60        # JPEG quality sent over USB (1..100), changeable from the viewer
WIFI_FILE   = 'wifi.json'   # written by the viewer; falls back to config.py (ssid, password)
AP_FILE     = 'ap.json'     # persisted AP credentials
AP_SSID_DEF = 'ESP32-CAM'   # default AP SSID
AP_PASS_DEF = '12345678'    # >=8 chars (WPA2); shorter -> open network
AP_AUTO     = True          # bring the AP up at boot (works alongside STA)
AP_CHANNEL  = 6             # only used while STA is not connected (STA forces its router channel)
AP_MAX_CLI  = 3             # max simultaneous AP clients
STALE_MS    = 100           # encode+send slower than this -> next frame is thrown away, grab a fresh one
CLIENT_TMO  = 1000          # ms: a client that accepted nothing for this long is dropped (browser reconnects by itself)
STALE_RECAPTURE = False     # True: after a slow cycle throw a frame away (costs one extra capture wait)
FB_COUNT    = 3             # camera frame buffers (more = capture overlaps encode/send)
EXPOSURE    = None          # None = sensor auto-exposure; int (e.g. 300) locks exposure = steady fps, darker image
BENCH       = True          # print raw sensor fps + JPEG encode time once at boot
GRAB_LATEST = False         # False = WHEN_EMPTY: sensor fills the 2nd buffer while we encode (capture overlaps work)
DRAIN_MS    = 60            # after sending a frame, keep pushing its remainder for up to this long

# ===========================================================================
#  Modes
#    name   : (pixel format, bytes/px from camera, wire fmt tag, codec)
#    codec  : 'jpeg'   = JPEG (best on slow WiFi)
#             'rle'    = gray, closed-loop delta + zero-run RLE, noise gate
#             'rle332' = RGB565 -> RGB332 (1 B/px), then lossless delta + RLE
#             'rleyuv' = YUV422 -> 4-bit Y/U/V (1 B/px, color kept), lossless delta + RLE
#             'chunk'  = 512 B chunk delta (full-quality RGB565)
# ===========================================================================
MODES = {
    'rgb':   (PixelFormat.RGB565,    2, 3, 'rle332'),
    'rgbhq': (PixelFormat.RGB565,    2, 0, 'chunk'),
    'yuv':   (PixelFormat.YUV422,    2, 4, 'rleyuv'),
    'gray':  (PixelFormat.GRAYSCALE, 1, 1, 'rle'),
    'jpg':   (PixelFormat.YUV422,    2, 5, 'jpeg'),   # color JPEG (YUV422 in)
    'jpgg':  (PixelFormat.GRAYSCALE, 1, 5, 'jpeg'),   # gray JPEG
}
JPEG_FMT = {'jpg': 'YCbYCr', 'jpgg': 'GRAY'}          # encoder input format
JPEG_Q   = (25, 35, 45, 55, 65)                       # WiFi JPEG quality levels (encode time does NOT depend on it, only size)
JPEG_Q0  = 2                                          # starting index
ENC_HI   = 0                                          # ms: JPEG encode slower than this -> lower quality (0 = off, useless here)
RES      = 'qvga'                                     # 'qvga' 320x240 (JPEG encode ~100 ms) | 'qqvga' 160x120 (~4x faster)
EXP_SWEEP = (60, 150, 300)                            # boot bench: exposure values tried to find the sensor fps limit
USB_PIX  = {PixelFormat.GRAYSCALE: 'GRAY', PixelFormat.YUV422: 'YCbYCr'}

# Noise gate: a pixel whose change is <= GATE (8-bit equivalent) is NOT sent.
# GATE adapts per frame between MIN and MAX: when the link is congested the gate
# rises (rougher but small frames), when it is clear it falls back.
KIND      = {'rle': 0, 'rle332': 1, 'rleyuv': 2, 'chunk': 0}   # how a delta is measured
GATE_MIN  = {'rle': 6,  'rle332': 0,   'rleyuv': 0,  'chunk': 0}
GATE_MAX  = {'rle': 80, 'rle332': 150, 'rleyuv': 64, 'chunk': 0}
GATE_STEP = {'rle': 6,  'rle332': 36,  'rleyuv': 16, 'chunk': 0}
CLEAR_N   = 12          # congestion-free frames before quality / gate goes back up
for _d in (KIND, GATE_MIN, GATE_MAX, GATE_STEP):
    _d['jpeg'] = 0

if RES == 'qqvga':
    FRAME_SIZE, WIDTH, HEIGHT = FrameSize.QQVGA, 160, 120
else:
    FRAME_SIZE, WIDTH, HEIGHT = FrameSize.QVGA, 320, 240
XCLK_HZ    = 20000000
EAGAIN     = 11

streams = []
pend    = {}    # socket -> [memoryview, sent_offset, tick_started]  (unsent tail of a packet)


def tune_sensor(c):
    """GC0308 auto-exposure lowers the frame rate in dim light. EXPOSURE (int) locks it."""
    if EXPOSURE is None:
        return
    for fn, arg in (('set_exposure_ctrl', 0), ('set_aec_value', EXPOSURE),
                    ('set_gain_ctrl', 1)):
        try:
            getattr(c, fn)(arg)
        except Exception as ex:
            print('sensor tune %s failed: %s' % (fn, ex))


def make_camera(name):
    gc.collect()
    c = Camera(
        frame_size=FRAME_SIZE,
        pixel_format=MODES[name][0],
        xclk_freq=XCLK_HZ,
        init=True,
        grab_mode=GrabMode.LATEST if GRAB_LATEST else GrabMode.WHEN_EMPTY,
        fb_count=FB_COUNT,
    )
    tune_sensor(c)
    return c


def bench():
    """Boot benchmark: raw sensor rate and JPEG encode time (printed once)."""
    r = None
    try:
        for _ in range(3):
            cam.capture()
        n = 30
        t = time.ticks_ms()
        for _ in range(n):
            r = cam.capture()
        dt = time.ticks_diff(time.ticks_ms(), t)
        print('BENCH sensor capture-only: %.1f fps (%d ms/frame)' % (n * 1000 / dt, dt // n))
        if HAVE_JPEG and r is not None and current_name in JPEG_FMT:
            e = jpeg.Encoder(height=HEIGHT, width=WIDTH, pixel_format=JPEG_FMT[current_name],
                             quality=30, rotation=0)
            t = time.ticks_ms()
            for _ in range(10):
                j = e.encode(r)
            print('BENCH jpeg encode: %d ms/frame, %d B' % (
                time.ticks_diff(time.ticks_ms(), t) // 10, len(j)))
    except Exception as ex:
        print('BENCH failed:', ex)
    # exposure sweep: does a locked (short) exposure raise the sensor fps?
    try:
        for v in EXP_SWEEP:
            try:
                cam.set_exposure_ctrl(0)
            except Exception:
                pass            # GC0308 driver may not support it
            cam.set_aec_value(v)
            for _ in range(2):
                cam.capture()
            n = 15
            t = time.ticks_ms()
            for _ in range(n):
                cam.capture()
            dt = time.ticks_diff(time.ticks_ms(), t)
            print('BENCH exposure %d: %.1f fps' % (v, n * 1000 / dt))
        try:
            cam.set_exposure_ctrl(1)
        except Exception:
            pass
    except Exception as ex:
        print('BENCH exposure sweep failed:', ex)


current_name = 'jpg' if HAVE_JPEG else 'rgb'
_t0 = time.ticks_ms()
cam = make_camera(current_name)
print('Camera init: %d ms' % time.ticks_diff(time.ticks_ms(), _t0))
if BENCH:
    bench()

# ===========================================================================
#  WiFi (non-blocking: the USB link works even when WiFi is not configured)
#  Three transports can be used at the same time:
#    USB (CH340), STA (joined to a router), AP (ESP32 acts as its own AP)
# ===========================================================================
station = network.WLAN(network.STA_IF)
station.active(True)
try:
    station.config(pm=station.PM_NONE)
except Exception:
    pass

ap = network.WLAN(network.AP_IF)

wifi_ssid = ''
wifi_up   = False
ap_ssid   = AP_SSID_DEF
ap_pass   = AP_PASS_DEF
ap_up     = False
ap_want   = AP_AUTO     # watchdog: restart the AP if it dies while this is True
srv       = None
poller    = select.poll()


def load_wifi():
    try:
        with open(WIFI_FILE) as f:
            d = json.load(f)
        return d['ssid'], d['password']
    except Exception:
        pass
    try:
        from config import ssid, password
        return ssid, password
    except Exception:
        return None, None


def save_wifi(ssid, pw):
    with open(WIFI_FILE, 'w') as f:
        json.dump({'ssid': ssid, 'password': pw}, f)


def load_ap():
    global ap_ssid, ap_pass
    try:
        with open(AP_FILE) as f:
            d = json.load(f)
        ap_ssid = d.get('ssid', AP_SSID_DEF)
        ap_pass = d.get('password', AP_PASS_DEF)
    except Exception:
        pass


def save_ap(ssid, pw):
    try:
        with open(AP_FILE, 'w') as f:
            json.dump({'ssid': ssid, 'password': pw}, f)
    except Exception:
        pass


def wifi_connect(ssid, pw):
    global wifi_ssid
    if not ssid:
        return
    wifi_ssid = ssid
    try:
        station.disconnect()
    except Exception:
        pass
    station.connect(ssid, pw)


def rssi():
    try:
        return station.status('rssi')
    except Exception:
        return 0


def start_server():
    global srv
    if srv is not None:
        return
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(socket.getaddrinfo('0.0.0.0', 80)[0][-1])
    s.listen(4)
    s.setblocking(False)
    poller.register(s, select.POLLIN)
    srv = s


def ap_start(ssid=None, pw=None):
    """Bring up the ESP32 soft-AP. Runs alongside STA (same channel)."""
    global ap_up, ap_ssid, ap_pass, ap_want
    if ssid:
        ap_ssid = ssid
    if pw is not None:
        ap_pass = pw
    ap_want = True
    try:
        gc.collect()
        ap.active(True)
        cfg = {'essid': ap_ssid}
        if len(ap_pass) >= 8:
            cfg['password'] = ap_pass
            cfg['authmode'] = network.AUTH_WPA_WPA2_PSK
        else:
            cfg['authmode'] = network.AUTH_OPEN
        ap.config(**cfg)
        # optional settings: not every firmware build knows them, never let them kill the AP
        try:
            ap.config(channel=AP_CHANNEL)
        except Exception:
            pass
        try:
            ap.config(max_clients=AP_MAX_CLI)
        except Exception:
            pass
        ap_up = True
        print('AP up: ssid=%s ip=%s' % (ap_ssid, ap.ifconfig()[0]))
        if srv is None:
            try:
                start_server()
            except OSError as e:
                print('server start failed:', e)
        return True
    except Exception as e:
        print('AP start failed:', e)
        ap_up = False
        return False


def ap_stop():
    global ap_up, ap_want
    ap_want = False
    try:
        ap.active(False)
    except Exception:
        pass
    ap_up = False
    print('AP down')


def wifi_check():
    global wifi_up, ap_up
    up = station.isconnected()
    if up and not wifi_up:
        print('WiFi STA: http://%s' % station.ifconfig()[0])
    wifi_up = up

    try:
        now_ap = ap.active()
    except Exception:
        now_ap = False

    # watchdog: AP died by itself -> bring it back
    if ap_want and not now_ap:
        print('AP died, restarting')
        ap_start()
        try:
            now_ap = ap.active()
        except Exception:
            now_ap = False

    if now_ap != ap_up:
        ap_up = now_ap
        if ap_up:
            print('AP: http://%s' % ap.ifconfig()[0])

    if (up or ap_up) and srv is None:
        try:
            start_server()
        except OSError as e:
            print('server start failed:', e)


_s, _p = load_wifi()
try:
    wifi_connect(_s, _p)
except Exception as _e:
    print('STA connect failed:', _e)

load_ap()
if AP_AUTO:
    ap_start()

# ===========================================================================
#  Viper kernels
# ===========================================================================
@micropython.viper
def rle_closed(cur: ptr8, prv: ptr8, out: ptr8, n: int, thr: int, kind: int) -> int:
    # Closed loop: prv always mirrors what the browser has.
    #   nonzero byte  -> (cur - prv) & 255, prv updated
    #   0x00, count   -> skip `count` positions (change within gate, prv kept)
    # kind 0: plain byte difference (gray)
    # kind 1: RGB332, worst channel difference scaled to 8 bit (r,g: x36  b: x85)
    # kind 2: two 4-bit values per byte (YUV44), worst nibble difference x16
    o = 0
    run = 0
    runidx = 0
    i = 0
    while i < n:
        c = int(cur[i])
        p = int(prv[i])
        s = 0
        if c != p:
            if kind == 0:
                s = (c - p) & 255
                if s > 127:
                    s = 256 - s
            elif kind == 1:
                a = (c >> 5) - (p >> 5)
                if a < 0:
                    a = -a
                s = a * 36
                a = ((c >> 2) & 7) - ((p >> 2) & 7)
                if a < 0:
                    a = -a
                a = a * 36
                if a > s:
                    s = a
                a = (c & 3) - (p & 3)
                if a < 0:
                    a = -a
                a = a * 85
                if a > s:
                    s = a
            else:
                a = (c >> 4) - (p >> 4)
                if a < 0:
                    a = -a
                s = a
                a = (c & 15) - (p & 15)
                if a < 0:
                    a = -a
                if a > s:
                    s = a
                s = s * 16
        if s <= thr:
            if run == 0:
                runidx = o + 1
                out[o] = 0
                o += 2
            run += 1
            out[runidx] = run
            if run >= 255:
                run = 0
        else:
            out[o] = (c - p) & 255
            prv[i] = c
            o += 1
            run = 0
        i += 1
    return o


@micropython.viper
def rgb565_to_332(src: ptr8, out: ptr8, n: int):
    # big-endian RGB565 -> RRRGGGBB
    i = 0
    j = 0
    while i + 1 < n:
        c = (int(src[i]) << 8) | int(src[i + 1])
        out[j] = (((c >> 13) & 7) << 5) | (((c >> 8) & 7) << 2) | ((c >> 3) & 3)
        j += 1
        i += 2


@micropython.viper
def yuv_to_44(src: ptr8, out: ptr8, n: int):
    # YUYV (Y0 U Y1 V) -> 2 bytes: (Y0h<<4 | Y1h), (Uh<<4 | Vh), rounded 4-bit values
    i = 0
    j = 0
    while i + 3 < n:
        y0 = (int(src[i]) + 8) >> 4
        if y0 > 15:
            y0 = 15
        u = (int(src[i + 1]) + 8) >> 4
        if u > 15:
            u = 15
        y1 = (int(src[i + 2]) + 8) >> 4
        if y1 > 15:
            y1 = 15
        v = (int(src[i + 3]) + 8) >> 4
        if v > 15:
            v = 15
        out[j] = (y0 << 4) | y1
        out[j + 1] = (u << 4) | v
        j += 2
        i += 4


# ===========================================================================
#  Runtime state
# ===========================================================================
FRAME_LEN   = 0      # bytes per camera frame
WIRE_LEN    = 0      # bytes per frame as the codec sees it
CODEC       = 'chunk'
GATE        = 0
ENCODERS    = {}     # WiFi jpeg.Encoder per quality level, created lazily
usb_encs    = {}     # USB jpeg.Encoder per pixel format
jq_idx      = JPEG_Q0   # current index into JPEG_Q
jq_fixed    = None   # None = adaptive WiFi JPEG quality, else a fixed index
GEOMETRY    = b''
prev        = bytearray()
OUTBUF      = None
QBUF        = None
prev_ok     = False
force_full  = True

CHUNK       = 512
N_CHUNKS    = 0
BITMAP_LEN  = 0
CHUNK_OFF   = []
EMPTY_DELTA = b''
NOISE_TOL   = 4000

# USB link state
uart        = None
stdin_poll  = None
usb_on      = False  # stream frames over USB (the viewer turns this on / keeps it alive)
usb_last    = 0
usb_rx      = b''
quit_flag   = False
mode_locked = False  # True: mode was chosen from USB, WiFi pages can't change it


def derive_wh(frame_len, name):
    """Fallback: guess (W, H) from a real frame length."""
    px = frame_len // MODES[name][1]
    for w, h in ((640, 480), (320, 240), (160, 120), (176, 144), (128, 96)):
        if w * h == px:
            return w, h
    return 320, px // 320


def setup_codec(frame_len):
    """Recompute every size/codec-dependent constant for current_name."""
    global FRAME_LEN, WIRE_LEN, CODEC, GATE, GEOMETRY, prev, OUTBUF, QBUF
    global N_CHUNKS, BITMAP_LEN, CHUNK_OFF, EMPTY_DELTA, prev_ok, force_full, jq_idx

    mode = MODES[current_name]
    CODEC = mode[3]
    GATE = GATE_MIN[CODEC]
    FRAME_LEN = frame_len
    WIRE_LEN = frame_len // 2 if CODEC in ('rle332', 'rleyuv') else frame_len

    N_CHUNKS   = (WIRE_LEN + CHUNK - 1) // CHUNK
    BITMAP_LEN = (N_CHUNKS + 7) // 8
    CHUNK_OFF  = [(i * CHUNK, min((i + 1) * CHUNK, WIRE_LEN)) for i in range(N_CHUNKS)]
    EMPTY_DELTA = b'\x01' + BITMAP_LEN.to_bytes(4, 'little') + bytes(BITMAP_LEN)
    GEOMETRY = bytes((
        WIDTH & 0xFF, WIDTH >> 8,
        HEIGHT & 0xFF, HEIGHT >> 8,
        mode[2],
    ))

    prev = None
    OUTBUF = None
    QBUF = None
    ENCODERS.clear()
    usb_encs.clear()
    jq_idx = JPEG_Q0 if jq_fixed is None else jq_fixed
    gc.collect()
    prev = bytearray(0 if CODEC == 'jpeg' else WIRE_LEN)
    if CODEC in ('rle', 'rle332', 'rleyuv'):
        OUTBUF = bytearray(WIRE_LEN * 3 // 2 + 16)
    if CODEC in ('rle332', 'rleyuv'):
        QBUF = bytearray(WIRE_LEN)
    prev_ok = False
    force_full = True


def close_streams():
    for c in streams:
        try:
            c.close()
        except OSError:
            pass
    del streams[:]
    pend.clear()


def change_format(new_name):
    """Switch mode. Camera is only re-initialised if the pixel format differs."""
    global cam, current_name

    if new_name == current_name:
        return

    print('Switching mode -> %s' % new_name)
    t0 = time.ticks_ms()
    close_streams()

    if MODES[new_name][0] != MODES[current_name][0]:
        try:
            cam.deinit()
        except Exception:
            pass
        cam = None
        gc.collect()
        time.sleep_ms(60)
        cam = make_camera(new_name)

    current_name = new_name
    setup_codec(WIDTH * HEIGHT * MODES[new_name][1])
    print('Now: %s %dx%d (%d B/frame) in %d ms' % (
        new_name, WIDTH, HEIGHT, FRAME_LEN, time.ticks_diff(time.ticks_ms(), t0)))


def adopt_actual(n):
    """Driver returned a different frame size than expected: follow it."""
    global WIDTH, HEIGHT
    WIDTH, HEIGHT = derive_wh(n, current_name)
    print('Adopting real frame: %s %dx%d (%d bytes)' % (current_name, WIDTH, HEIGHT, n))
    close_streams()
    setup_codec(n)


setup_codec(WIDTH * HEIGHT * MODES[current_name][1])
print('Frame: %s %dx%d (%d bytes, wire %d)' % (current_name, WIDTH, HEIGHT, FRAME_LEN, WIRE_LEN))

# ===========================================================================
#  WiFi page (HTML + mode switcher + multi-format decoder)
#  Wire fmt tags: 0=RGB565  1=GRAY  2=YUV422  3=RGB332  4=YUV44 (4-bit Y/U/V)  5=JPEG
#  Packet types : 0=full frame  1=chunk delta  2=RLE delta  3=JPEG
# ===========================================================================
HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ESP32 Camera</title>
<style>
  html,body{height:100%;margin:0;background:#111;color:#eee;
            font-family:system-ui,Arial,sans-serif;
            display:flex;align-items:center;justify-content:center}
  .wrap{width:100%;height:100%;padding:12px;box-sizing:border-box;text-align:center;
        display:flex;flex-direction:column;overflow:auto}
  h1{font-size:18px;font-weight:600;margin:0 0 10px}
  .bar{display:flex;gap:8px;justify-content:center;margin-bottom:10px;flex-wrap:wrap}
  .bar label{font-size:13px;color:#9aa;align-self:center}
  select{background:#222;color:#eee;border:1px solid #444;border-radius:4px;
         padding:6px 10px;font-size:13px}
  canvas{flex:1 1 0;min-height:0;width:100%;object-fit:contain;display:block;
         background:#000;border:1px solid #333;border-radius:6px}
  #status{margin-top:8px;font-size:13px;color:#9aa}
  #net{margin-top:6px;font-size:12px;color:#789}
</style>
</head>
<body>
<div class="wrap">
  <h1>ESP32 Camera</h1>
  <div class="bar">
    <label for="fmt">Mode:</label>
    <select id="fmt">
      <option value="rgb">Color RGB332 (compressed, fast)</option>
      <option value="gray">Grayscale (compressed, fastest)</option>
      <option value="yuv">Color YUV 4-bit (compressed)</option>
      <option value="rgbhq">Color RGB565 (full quality, slow)</option>
      <option value="jpg">Color JPEG (constant size)</option>
      <option value="jpgg">Gray JPEG (constant size)</option>
    </select>
    <label for="scale">Scale:</label>
    <select id="scale">
      <option value="fit">Fit window</option>
      <option value="1">1x</option>
      <option value="2">2x</option>
      <option value="3">3x</option>
      <option value="4">4x</option>
    </select>
    <label><input type="checkbox" id="smooth"> Smooth</label>
  </div>
  <canvas id="c" width="320" height="240"></canvas>
  <div id="status">Connecting&hellip;</div>
  <div id="net"></div>
</div>
<script>
(function () {
  var canvas   = document.getElementById('c');
  var ctx      = canvas.getContext('2d', { alpha: false });
  var statusEl = document.getElementById('status');
  var fmtSel   = document.getElementById('fmt');
  var scaleSel = document.getElementById('scale');
  var smoothChk = document.getElementById('smooth');
  var userSmooth = null;   // null = follow the mode default (JPEG smooth, raw sharp)

  // All scaling happens here in the browser; the ESP32 stream is untouched.
  function applyView(geomChanged) {
    if (geomChanged && userSmooth === null) smoothChk.checked = (FMT === 5);
    canvas.style.imageRendering = smoothChk.checked ? 'auto' : 'pixelated';
    var s = scaleSel.value;
    if (s === 'fit' || !W) {
      canvas.style.flex = '1 1 0';
      canvas.style.width = '100%';
      canvas.style.height = '';
      canvas.style.alignSelf = 'stretch';
    } else {
      var k = parseInt(s, 10);
      canvas.style.flex = 'none';
      canvas.style.width = (W * k) + 'px';
      canvas.style.height = (H * k) + 'px';
      canvas.style.alignSelf = 'center';
    }
  }
  scaleSel.addEventListener('change', function () { applyView(false); });
  smoothChk.addEventListener('change', function () { userSmooth = smoothChk.checked; applyView(false); });

  var CHUNK = 512;

  var W = 0, H = 0, FRAME = 0, PIXELS = 0, FMT = 0;
  var N_CHUNKS = 0, BITMAP_LEN = 0;

  var frameBuf = null;
  var imgData  = null, rgba = null;

  var LUT3 = [], LUT2 = [];
  for (var k = 0; k < 8; k++) LUT3.push(Math.round(k * 255 / 7));
  for (var k2 = 0; k2 < 4; k2++) LUT2.push(k2 * 85);

  // stream state machine
  var ST_GEOM = 0, ST_HEAD = 1, ST_PAYLOAD = 2;
  var state = ST_GEOM;

  var geomBuf = new Uint8Array(5), geomFill = 0;   // W W H H FMT
  var headBuf = new Uint8Array(5), headFill = 0;
  var payloadType = 0, payloadLen = 0;
  var payloadBuf = null, payloadFill = 0;
  var sharedPayload = null, maxPayload = 0;

  var frames = 0, bytesIn = 0, fpsT = performance.now();
  var lastData = performance.now();

  function initGeometry(w, h, fmtByte) {
    W = w; H = h; FMT = fmtByte;
    PIXELS = W * H;
    // gray + rgb332 + yuv44 = 1 B/px on the wire; rgb565 + yuv422 = 2 B/px
    FRAME = (FMT === 1 || FMT === 3 || FMT === 4) ? PIXELS : PIXELS * 2;
    N_CHUNKS  = Math.ceil(FRAME / CHUNK);
    BITMAP_LEN = Math.ceil(N_CHUNKS / 8);
    maxPayload = FRAME * 2 + 64;     // covers full, chunk and worst-case RLE
    sharedPayload = new Uint8Array(maxPayload);

    canvas.width  = W;
    canvas.height = H;
    applyView(true);
    frameBuf = new Uint8Array(FRAME);
    imgData  = ctx.createImageData(W, H);
    rgba     = imgData.data;
  }

  // ---- format-specific renderers ----------------------------------------
  function renderRGB565() {
    var j = 0;
    for (var i = 0; i < FRAME; i += 2) {
      var c = (frameBuf[i] << 8) | frameBuf[i + 1];
      rgba[j++] = ((c >> 11) & 0x1F) << 3;
      rgba[j++] = ((c >>  5) & 0x3F) << 2;
      rgba[j++] = ( c        & 0x1F) << 3;
      rgba[j++] = 255;
    }
  }

  function renderRGB332() {
    var j = 0;
    for (var i = 0; i < PIXELS; i++) {
      var v = frameBuf[i];
      rgba[j++] = LUT3[(v >> 5) & 7];
      rgba[j++] = LUT3[(v >> 2) & 7];
      rgba[j++] = LUT2[v & 3];
      rgba[j++] = 255;
    }
  }

  function renderGray() {
    var j = 0;
    for (var i = 0; i < PIXELS; i++) {
      var v = frameBuf[i];
      rgba[j++] = v;
      rgba[j++] = v;
      rgba[j++] = v;
      rgba[j++] = 255;
    }
  }

  function renderYUV422() {
    // YUYV byte order: Y0 U0 Y1 V0 (2 pixels per 4 bytes)
    var j = 0;
    for (var i = 0; i < FRAME; i += 4) {
      var y0 = frameBuf[i],     u = frameBuf[i + 1],
          y1 = frameBuf[i + 2], v = frameBuf[i + 3];
      var d = u - 128, e = v - 128;
      var c0 = y0 - 16, c1 = y1 - 16;
      var r, g, b;
      r = (298*c0 + 409*e + 128) >> 8;
      g = (298*c0 - 100*d - 208*e + 128) >> 8;
      b = (298*c0 + 516*d + 128) >> 8;
      rgba[j++] = r < 0 ? 0 : r > 255 ? 255 : r;
      rgba[j++] = g < 0 ? 0 : g > 255 ? 255 : g;
      rgba[j++] = b < 0 ? 0 : b > 255 ? 255 : b;
      rgba[j++] = 255;
      r = (298*c1 + 409*e + 128) >> 8;
      g = (298*c1 - 100*d - 208*e + 128) >> 8;
      b = (298*c1 + 516*d + 128) >> 8;
      rgba[j++] = r < 0 ? 0 : r > 255 ? 255 : r;
      rgba[j++] = g < 0 ? 0 : g > 255 ? 255 : g;
      rgba[j++] = b < 0 ? 0 : b > 255 ? 255 : b;
      rgba[j++] = 255;
    }
  }

  function renderYUV44() {
    // 2 bytes per 2 px: (Y0h<<4 | Y1h), (Uh<<4 | Vh); values are n<<4
    var j = 0;
    for (var i = 0; i < FRAME; i += 2) {
      var a = frameBuf[i], b = frameBuf[i + 1];
      var d = (b & 0xF0) - 128, e = ((b & 0x0F) << 4) - 128;
      var c0 = (a & 0xF0) - 16, c1 = ((a & 0x0F) << 4) - 16;
      var r, g, bl;
      r  = (298*c0 + 409*e + 128) >> 8;
      g  = (298*c0 - 100*d - 208*e + 128) >> 8;
      bl = (298*c0 + 516*d + 128) >> 8;
      rgba[j++] = r < 0 ? 0 : r > 255 ? 255 : r;
      rgba[j++] = g < 0 ? 0 : g > 255 ? 255 : g;
      rgba[j++] = bl < 0 ? 0 : bl > 255 ? 255 : bl;
      rgba[j++] = 255;
      r  = (298*c1 + 409*e + 128) >> 8;
      g  = (298*c1 - 100*d - 208*e + 128) >> 8;
      bl = (298*c1 + 516*d + 128) >> 8;
      rgba[j++] = r < 0 ? 0 : r > 255 ? 255 : r;
      rgba[j++] = g < 0 ? 0 : g > 255 ? 255 : g;
      rgba[j++] = bl < 0 ? 0 : bl > 255 ? 255 : bl;
      rgba[j++] = 255;
    }
  }

  function render() {
    if (FMT === 0)      renderRGB565();
    else if (FMT === 1) renderGray();
    else if (FMT === 2) renderYUV422();
    else if (FMT === 4) renderYUV44();
    else                renderRGB332();
    ctx.putImageData(imgData, 0, 0);
  }

  // ---- decode ------------------------------------------------------------
  var jpgSeq = 0, jpgShown = 0;
  function drawJpeg(payload) {
    var my = ++jpgSeq;
    var blob = new Blob([payload.slice()], { type: 'image/jpeg' });  // slice() copies
    createImageBitmap(blob).then(function (bmp) {
      if (my > jpgShown) {            // never draw an older frame over a newer one
        jpgShown = my;
        ctx.drawImage(bmp, 0, 0);
        frames++;
      }
      bmp.close();
    }).catch(function () {});
  }

  function applyFrame(type, payload) {
    if (type === 3) { drawJpeg(payload); return; }
    if (type === 0) {
      frameBuf.set(payload.subarray(0, FRAME));
    } else if (type === 1) {
      var bitmap = payload.subarray(0, BITMAP_LEN);
      var off = BITMAP_LEN;
      for (var i = 0; i < N_CHUNKS; i++) {
        if (bitmap[i >> 3] & (1 << (i & 7))) {
          var cOff = i * CHUNK;
          var cEnd = cOff + CHUNK;
          if (cEnd > FRAME) cEnd = FRAME;
          var len = cEnd - cOff;
          frameBuf.set(payload.subarray(off, off + len), cOff);
          off += len;
        }
      }
    } else {
      // RLE delta: 0,n = skip n px ; v = add v to px (Uint8Array wraps mod 256)
      var p = 0, q = 0, n2 = payload.length;
      while (q < n2 && p < FRAME) {
        var v = payload[q++];
        if (v === 0) {
          p += payload[q++];
        } else {
          frameBuf[p] = frameBuf[p] + v;
          p++;
        }
      }
    }
    frames++;
    render();
  }

  function feed(chunk) {
    bytesIn += chunk.length;
    lastData = performance.now();
    var off = 0;
    while (off < chunk.length) {
      if (state === ST_GEOM) {
        var n = Math.min(5 - geomFill, chunk.length - off);
        geomBuf.set(chunk.subarray(off, off + n), geomFill);
        geomFill += n; off += n;
        if (geomFill === 5) {
          initGeometry(geomBuf[0] | (geomBuf[1] << 8),
                       geomBuf[2] | (geomBuf[3] << 8),
                       geomBuf[4]);
          state = ST_HEAD;
        }
      } else if (state === ST_HEAD) {
        var n = Math.min(5 - headFill, chunk.length - off);
        headBuf.set(chunk.subarray(off, off + n), headFill);
        headFill += n; off += n;
        if (headFill === 5) {
          payloadType = headBuf[0];
          payloadLen  = headBuf[1] | (headBuf[2] << 8) |
                        (headBuf[3] << 16) | (headBuf[4] << 24);
          if (payloadLen > 0x4000000) throw new Error('corrupt stream');
          if (payloadLen > sharedPayload.length) {
            // bigger frame than expected (higher res / quality): grow, never fail
            sharedPayload = new Uint8Array(Math.max(payloadLen, sharedPayload.length * 2));
          }
          payloadBuf  = sharedPayload.subarray(0, payloadLen);
          payloadFill = 0;
          state = ST_PAYLOAD;
        }
      } else {
        var n = Math.min(payloadLen - payloadFill, chunk.length - off);
        payloadBuf.set(chunk.subarray(off, off + n), payloadFill);
        payloadFill += n; off += n;
        if (payloadFill === payloadLen) {
          applyFrame(payloadType, payloadBuf);
          state = ST_HEAD;
          headFill = 0;
        }
      }
    }
  }

  // ---- stream loop -------------------------------------------------------
  // gen: every run() bumps it; stale loops never schedule a reconnect.
  var gen = 0;
  var abortCtl = null;
  var wantedFormat = 'jpg';
  fmtSel.value = wantedFormat;

  function run() {
    var my = ++gen;
    if (abortCtl) { try { abortCtl.abort(); } catch (e) {} }
    var ctl = new AbortController();
    abortCtl = ctl;

    geomFill = 0; headFill = 0; payloadFill = 0; state = ST_GEOM;
    lastData = performance.now();

    fetch('/stream?fmt=' + wantedFormat, { cache: 'no-store', signal: ctl.signal })
      .then(function (res) {
        if (!res.ok || !res.body) throw new Error('bad response');
        var reader = res.body.getReader();
        return (function pump() {
          return reader.read().then(function (r) {
            if (my !== gen || r.done) return;
            feed(r.value);
            return pump();
          });
        })();
      })
      .catch(function () {})
      .then(function () {
        if (my !== gen) return;
        setTimeout(function () { if (my === gen) run(); }, 300);
      });
  }

  // ---- stats + watchdog --------------------------------------------------
  (function tick() {
    var now = performance.now();
    if (now - fpsT >= 1000) {
      var fps  = frames * 1000 / (now - fpsT);
      var kbps = bytesIn * 8 / (now - fpsT);
      frames = 0; bytesIn = 0; fpsT = now;
      var names = ['RGB565', 'GRAY', 'YUV422', 'RGB332', 'YUV44', 'JPEG'];
      statusEl.textContent = names[FMT] + '  ' + W + 'x' + H + '  ' +
                             fps.toFixed(1) + ' fps  ' + kbps.toFixed(0) + ' kbps';
    }
    if (now - lastData > 4000) {   // stalled stream: restart it
      lastData = now;
      run();
    }
    requestAnimationFrame(tick);
  })();

  // ---- network banner (STA / AP) ----------------------------------------
  (function netTick() {
    fetch('/net', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
      var p = [];
      if (d.sta_up) p.push('STA ' + d.sta_ssid + ' ' + d.sta_ip + ' (' + d.rssi + ' dBm)');
      if (d.ap_up)  p.push('AP '  + d.ap_ssid  + ' ' + d.ap_ip);
      if (!p.length) p.push('No WiFi');
      document.getElementById('net').textContent = p.join('  |  ');
    }).catch(function () {});
    setTimeout(netTick, 5000);
  })();

  fmtSel.addEventListener('change', function () {
    wantedFormat = fmtSel.value;
    statusEl.textContent = 'Switching to ' + fmtSel.options[fmtSel.selectedIndex].text + '...';
    run();
  });

  run();
})();
</script>
</body>
</html>
"""

HTML_HEADER = (
    b'HTTP/1.1 200 OK\r\n'
    b'Content-Type: text/html; charset=utf-8\r\n'
    b'Content-Length: %d\r\n'
    b'Cache-Control: no-store\r\n'
    b'Connection: close\r\n\r\n' % len(HTML)
)
NOT_FOUND = (
    b'HTTP/1.1 404 Not Found\r\n'
    b'Content-Length: 0\r\n'
    b'Connection: close\r\n\r\n'
)
STREAM_HEADER = (
    b'HTTP/1.1 200 OK\r\n'
    b'Content-Type: application/octet-stream\r\n'
    b'Cache-Control: no-store, no-cache, must-revalidate\r\n'
    b'Pragma: no-cache\r\n'
    b'Access-Control-Allow-Origin: *\r\n'
    b'Connection: close\r\n\r\n'
)

# ===========================================================================
#  Encoders (WiFi)
# ===========================================================================
frame_count = 0
FORCE_FULL_EVERY = 240


def chunk_diff(off, end, fb, pv):
    acc = 0
    i = off
    while i + 4 <= end:
        a = fb[i] | (fb[i+1] << 8) | (fb[i+2] << 16) | (fb[i+3] << 24)
        b = pv[i] | (pv[i+1] << 8) | (pv[i+2] << 16) | (pv[i+3] << 24)
        d = a ^ b
        acc += (d & 0xFF) + ((d >> 8) & 0xFF) + ((d >> 16) & 0xFF) + ((d >> 24) & 0xFF)
        if acc > NOISE_TOL:
            return acc
        i += 4
    while i < end:
        d = fb[i] - pv[i]
        acc += d if d >= 0 else -d
        if acc > NOISE_TOL:
            return acc
        i += 1
    return acc


def encode_chunk(fb):
    global force_full, prev_ok

    if force_full or not prev_ok:
        force_full = False
        prev_ok = True
        prev[:] = fb
        return b'\x00' + WIRE_LEN.to_bytes(4, 'little') + fb

    if fb == prev:
        return EMPTY_DELTA

    bitmap = bytearray(BITMAP_LEN)
    changed = []
    for i in range(N_CHUNKS):
        off, end = CHUNK_OFF[i]
        if chunk_diff(off, end, fb, prev) > NOISE_TOL:
            bitmap[i >> 3] |= 1 << (i & 7)
            changed.append((off, end))

    if len(changed) * 4 > N_CHUNKS * 3:
        prev[:] = fb
        return b'\x00' + WIRE_LEN.to_bytes(4, 'little') + fb

    if not changed:
        return EMPTY_DELTA

    parts = [fb[o:e] for o, e in changed]
    prev[:] = fb
    payload = bytes(bitmap) + b''.join(parts)
    return b'\x01' + len(payload).to_bytes(4, 'little') + payload


def get_enc():
    """jpeg.Encoder for the current mode + WiFi quality level (None if unavailable)."""
    e = ENCODERS.get(jq_idx)
    if e is None:
        try:
            e = jpeg.Encoder(height=HEIGHT, width=WIDTH,
                             pixel_format=JPEG_FMT[current_name],
                             quality=JPEG_Q[jq_idx], rotation=0)
        except Exception as ex:
            print('jpeg encoder failed:', ex)
            return None
        ENCODERS[jq_idx] = e
    return e


def jpeg_encode(fb):
    """Raw JPEG bytes for the WiFi stream, or None."""
    enc = get_enc()
    if enc is None:
        return None
    try:
        return enc.encode(fb)
    except Exception as ex:
        print('jpeg encode error:', ex)
        gc.collect()
        return None


def encode_rle(src):
    """src = WIRE_LEN bytes (gray frame, RGB332 or YUV44 buffer). Closed-loop delta + RLE."""
    global force_full, prev_ok

    if force_full or not prev_ok:
        force_full = False
        prev_ok = True
        prev[:] = src
        return b'\x00' + WIRE_LEN.to_bytes(4, 'little') + bytes(src)

    o = rle_closed(src, prev, OUTBUF, WIRE_LEN, GATE, KIND[CODEC])
    return b'\x02' + o.to_bytes(4, 'little') + bytes(memoryview(OUTBUF)[:o])


def set_mode(name):
    """Switch mode (with JPEG fallbacks). Used by WiFi pages and by USB commands."""
    if name not in MODES:
        return
    if MODES[name][3] == 'jpeg' and not HAVE_JPEG:
        name = 'rgb'
    if name != current_name:
        change_format(name)
    if CODEC == 'jpeg' and get_enc() is None:
        change_format('rgb')    # encoder refused this format: fall back


# ===========================================================================
#  Non-blocking WiFi sending: a slow client (AP!) never stalls the main loop.
#  Unsent bytes of a packet are kept per socket in `pend`; while any client is
#  still draining, the next WiFi frame is simply not encoded (encoders only
#  update `prev` when they encode, so delta codecs stay in sync).
# ===========================================================================
def drop_clients(dead):
    if not dead:
        return
    print('dropping %d stuck/dead client(s), free heap %d' % (len(dead), gc.mem_free()))
    for c in dead:
        pend.pop(c, None)
        if c in streams:
            streams.remove(c)
        try:
            c.close()
        except OSError:
            pass
    gc.collect()


def flush_pending():
    """Push unsent bytes. Drops clients that are dead or have been stuck too long."""
    if not pend:
        return
    dead = None
    now = time.ticks_ms()
    for c in list(pend.keys()):
        p = pend[c]
        try:
            n = c.send(p[0][p[1]:])
            if n is None:
                n = 0
        except OSError as e:
            if e.args and e.args[0] == EAGAIN:
                n = 0
            else:
                print('flush error:', e)
                if dead is None:
                    dead = []
                dead.append(c)
                continue
        if n:
            p[1] += n
            p[2] = now
            if p[1] >= len(p[0]):
                del pend[c]
                continue
        if time.ticks_diff(now, p[2]) > CLIENT_TMO:
            if dead is None:
                dead = []
            dead.append(c)
    drop_clients(dead)


def send_packet(packet):
    """Send to every client without blocking; the remainder is queued in `pend`."""
    dead = None
    mv = None
    L = len(packet)
    now = time.ticks_ms()
    for c in streams:
        try:
            n = c.send(packet)
            if n is None:
                n = 0
        except OSError as e:
            if e.args and e.args[0] == EAGAIN:
                n = 0
            else:
                print('send error:', e)
                if dead is None:
                    dead = []
                dead.append(c)
                continue
        if n < L:
            if mv is None:
                mv = memoryview(packet)
            pend[c] = [mv, n, now]
    drop_clients(dead)


# ===========================================================================
#  USB link: CH340 -> UART0.  ESP32 -> PC: JPEG frames + status messages.
#  PC -> ESP32: one JSON command per line.
#    A5 5A C3 3C | len u32 LE | JPEG        (frame)
#    A5 5A C3 3D | len u32 LE | JSON text   (status / error)
# ===========================================================================
MAGIC_JPG = b'\xa5\x5a\xc3\x3c'
MAGIC_MSG = b'\xa5\x5a\xc3\x3d'


def usb_write(b):
    if uart is not None:
        uart.write(b)
    else:
        sys.stdout.buffer.write(b)


def usb_read():
    if uart is not None:
        n = uart.any()
        if not n:
            return b''
        return uart.read(n) or b''
    data = b''
    while len(data) < 256 and stdin_poll.poll(0):
        data += sys.stdin.buffer.read(1)
    return data


def usb_send_jpeg(j):
    usb_write(MAGIC_JPG + len(j).to_bytes(4, 'little'))
    usb_write(j)


def usb_send_msg(obj):
    b = json.dumps(obj).encode()
    usb_write(MAGIC_MSG + len(b).to_bytes(4, 'little') + b)


def usb_send_status():
    up = station.isconnected()
    usb_send_msg({
        't': 'status',
        'mode': current_name,
        'locked': mode_locked,
        'usb': usb_on,
        'q': 'auto' if jq_fixed is None else jq_fixed,
        'qlevel': JPEG_Q[jq_idx],
        'usbq': USB_QUALITY,
        'jpeg': HAVE_JPEG,
        'baud': BAUD if uart is not None else 115200,
        'ssid': wifi_ssid,
        'wifi': up,
        'ip': station.ifconfig()[0] if up else '',
        'rssi': rssi() if up else 0,
        'ap': ap_up,
        'ap_ssid': ap_ssid,
        'ap_ip': ap.ifconfig()[0] if ap_up else '',
    })


def usb_jpeg(fb):
    """JPEG for the USB link (own encoder at USB_QUALITY). None for RGB565 modes."""
    if not HAVE_JPEG:
        return None
    name = USB_PIX.get(MODES[current_name][0])
    if name is None:
        return None
    try:
        e = usb_encs.get(name)
        if e is None:
            e = jpeg.Encoder(height=HEIGHT, width=WIDTH, pixel_format=name,
                             quality=USB_QUALITY, rotation=0)
            usb_encs[name] = e
        return e.encode(fb)
    except Exception as ex:
        print('usb jpeg error:', ex)
        gc.collect()
        return None


def handle_cmd(c):
    global usb_on, quit_flag, mode_locked, jq_fixed, jq_idx, USB_QUALITY
    cmd = c.get('cmd')
    if cmd == 'usb':
        usb_on = bool(c.get('on'))
        if usb_on and MODES[current_name][0] == PixelFormat.RGB565:
            mode_locked = True      # RGB565 can't be sent as JPEG: switch to a JPEG mode
            set_mode('jpg' if HAVE_JPEG else 'gray')
    elif cmd == 'wifi':
        s = c.get('ssid')
        if s:
            p = c.get('password') or ''
            save_wifi(s, p)
            wifi_connect(s, p)
    elif cmd == 'ap':
        if c.get('on'):
            s = c.get('ssid') or ap_ssid
            p = c.get('password') if 'password' in c else ap_pass
            save_ap(s, p or '')
            ap_start(s, p)
        else:
            ap_stop()
    elif cmd == 'mode':
        m = c.get('mode')
        if m == 'free':
            mode_locked = False
        elif m in MODES:
            mode_locked = True
            set_mode(m)
    elif cmd == 'quality':
        q = c.get('q')
        if q == 'auto':
            jq_fixed = None
        else:
            jq_fixed = max(0, min(len(JPEG_Q) - 1, int(q)))
            jq_idx = jq_fixed
    elif cmd == 'usbq':
        USB_QUALITY = max(1, min(100, int(c.get('q'))))
        usb_encs.clear()
    elif cmd == 'repl':
        quit_flag = True
        return
    elif cmd == 'reboot':
        machine.reset()
    usb_send_status()       # every command (incl. 'get') is answered with the status


def usb_poll():
    global usb_rx, usb_last, quit_flag
    data = usb_read()
    if not data:
        return
    if b'\x03' in data:         # Ctrl-C: give the REPL back
        quit_flag = True
        return
    usb_rx += data
    if len(usb_rx) > 2048:
        usb_rx = b''
        return
    while b'\n' in usb_rx:
        line, usb_rx = usb_rx.split(b'\n', 1)
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        usb_last = time.ticks_ms()
        try:
            handle_cmd(c)
        except Exception as e:
            usb_send_msg({'t': 'error', 'msg': str(e)})


def enter_fast_usb():
    """After a short safe window at 115200, move UART0 to BAUD for the USB link."""
    global uart, stdin_poll
    print('USB: switching to %d baud in %d s (Ctrl-C now keeps the normal REPL)' % (BAUD, SAFE_S))
    for _ in range(SAFE_S * 10):
        time.sleep_ms(100)
        wifi_check()
    try:
        os.dupterm(None)
    except Exception:
        pass
    uart = None
    err = None
    # some ports refuse custom buffer sizes on the REPL UART: try fewer arguments
    for kw in (dict(baudrate=BAUD, txbuf=16384, rxbuf=1024),
               dict(baudrate=BAUD, txbuf=16384),
               dict(baudrate=BAUD)):
        try:
            uart = machine.UART(0, **kw)
            break
        except Exception as e:
            err = e
    if uart is None:
        try:
            u = machine.UART(0, 115200)
            u.init(baudrate=BAUD)
            uart = u
        except Exception as e:
            err = e
    if uart is None:
        print('USB: fast UART failed (%s), staying at 115200' % err)
    else:
        print('USB: UART0 at %d baud' % BAUD)
    if uart is None:
        stdin_poll = select.poll()
        stdin_poll.register(sys.stdin, select.POLLIN)


def restore_repl():
    global uart
    try:
        if uart is not None:
            uart.deinit()
    except Exception:
        pass
    if uart is not None:
        try:
            os.dupterm(machine.UART(0, 115200))
        except Exception:
            pass
    uart = None


# ===========================================================================
#  HTTP handling (WiFi)
# ===========================================================================
def read_request(c):
    # short timeout: browsers on the AP open speculative connections that never
    # send anything, a long wait here would freeze the whole stream
    c.settimeout(0.3)
    data = b''
    while b'\r\n\r\n' not in data and len(data) < 1024:
        try:
            chunk = c.recv(512)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    return data


def handle_new(c):
    global force_full
    req = read_request(c)

    if b'GET /stream' in req:
        if b'fmt=jpgg' in req:
            fmt = 'jpgg'
        elif b'fmt=jpg' in req:
            fmt = 'jpg'
        elif b'fmt=rgbhq' in req:
            fmt = 'rgbhq'
        elif b'fmt=gray' in req:
            fmt = 'gray'
        elif b'fmt=yuv' in req:
            fmt = 'yuv'
        else:
            fmt = 'rgb'

        if not mode_locked:         # a mode picked from USB wins over WiFi pages
            set_mode(fmt)

        try:
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.settimeout(1.5)
            c.sendall(STREAM_HEADER + GEOMETRY)
            c.setblocking(False)    # from now on frames go out via send_packet / flush_pending
            streams.append(c)
            force_full = True
            return
        except OSError:
            pass
    elif b'GET /net' in req:
        up = station.isconnected()
        info = json.dumps({
            'sta_up':   up,
            'sta_ip':   station.ifconfig()[0] if up else '',
            'sta_ssid': wifi_ssid,
            'rssi':     rssi() if up else 0,
            'ap_up':    ap_up,
            'ap_ssid':  ap_ssid,
            'ap_ip':    ap.ifconfig()[0] if ap_up else '',
        }).encode()
        try:
            c.settimeout(3.0)
            c.sendall(
                b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                b'Access-Control-Allow-Origin: *\r\n'
                b'Content-Length: %d\r\nCache-Control: no-store\r\n'
                b'Connection: close\r\n\r\n' % len(info) + info)
        except OSError:
            pass
    elif b'GET / ' in req or b'GET /index' in req:
        try:
            c.settimeout(3.0)
            c.sendall(HTML_HEADER + HTML)
        except OSError:
            pass
    elif req:
        try:
            c.settimeout(3.0)
            c.sendall(NOT_FOUND)
        except OSError:
            pass

    try:
        c.close()
    except OSError:
        pass


# ===========================================================================
#  Main loop
# ===========================================================================
bad_frames = 0
st_t = time.ticks_ms()
st_n = 0
st_f = 0
st_cap = 0
st_enc = 0
st_snd = 0
st_bytes = 0
st_drop = 0
wifi_t = time.ticks_ms()
stale = False       # last cycle was slow: throw the buffered frame away, grab a fresh one
clean = 0           # consecutive congestion-free frames

try:
    enter_fast_usb()

    while True:
        # One bad iteration (MemoryError, socket error, ...) must never kill the
        # whole script: log it, free memory, carry on.
        try:
            for obj, _ in poller.poll(0 if (streams or usb_on) else 20):
                if obj is srv:
                    try:
                        client, _ = srv.accept()
                    except OSError:
                        continue
                    try:
                        handle_new(client)
                    except Exception as e:
                        print('client error:', e)
                        try:
                            client.close()
                        except Exception:
                            pass
                        gc.collect()

            usb_poll()
            if quit_flag:
                break

            now = time.ticks_ms()
            if usb_on and time.ticks_diff(now, usb_last) > 6000:
                usb_on = False          # viewer closed without saying goodbye
            if time.ticks_diff(now, wifi_t) >= 1000:
                wifi_t = now
                wifi_check()

            if streams:
                flush_pending()

            if not streams and not usb_on:
                stale = False
                continue

            if stale and STALE_RECAPTURE:
                # last frame took > STALE_MS: the buffered frame is old, record a new one
                stale = False
                cam.capture()

            t_a = time.ticks_ms()
            raw = cam.capture()
            if not raw:
                continue

            if len(raw) != FRAME_LEN:
                bad_frames += 1
                if bad_frames >= 20:
                    bad_frames = 0
                    adopt_actual(len(raw))
                continue
            bad_frames = 0

            # only the chunk codec needs a private copy; the others read the camera buffer in place
            fb = bytes(raw) if CODEC == 'chunk' else raw
            t_b = time.ticks_ms()

            # ---- WiFi packet ------------------------------------------------
            packet = None
            jbytes = None
            dropped = False
            if streams:
                if pend:
                    # a client is still draining the previous frame: skip this one
                    dropped = True
                else:
                    frame_count += 1
                    if frame_count % FORCE_FULL_EVERY == 0:
                        force_full = True

                    if CODEC == 'jpeg':
                        jbytes = jpeg_encode(fb)
                        if jbytes is not None:
                            packet = b'\x03' + len(jbytes).to_bytes(4, 'little') + jbytes
                    elif CODEC == 'chunk':
                        packet = encode_chunk(fb)
                    elif CODEC == 'rle':
                        packet = encode_rle(fb)
                    elif CODEC == 'rle332':
                        rgb565_to_332(fb, QBUF, FRAME_LEN)
                        packet = encode_rle(QBUF)
                    else:
                        yuv_to_44(fb, QBUF, FRAME_LEN)
                        packet = encode_rle(QBUF)

            # ---- USB frame (reuses the WiFi JPEG when there is one) ---------
            if usb_on:
                if jbytes is None:
                    jbytes = usb_jpeg(fb)
                if jbytes is not None:
                    usb_send_jpeg(jbytes)

            t_c = time.ticks_ms()
            snd = 0

            if packet is not None:
                send_packet(packet)
                td = time.ticks_ms()
                # the sensor is already filling the next buffer: use the time to push the rest out
                while pend and time.ticks_diff(time.ticks_ms(), td) < DRAIN_MS:
                    time.sleep_ms(1)
                    flush_pending()
                snd = time.ticks_diff(time.ticks_ms(), t_c)

            # ---- adapt quality / gate to congestion -------------------------
            if streams and (packet is not None or dropped):
                congested = dropped or bool(pend) or (
                    CODEC == 'jpeg' and ENC_HI and time.ticks_diff(t_c, t_b) > ENC_HI)
                if CODEC == 'jpeg':
                    if jq_fixed is None:
                        if congested:
                            clean = 0
                            if jq_idx > 0:
                                jq_idx -= 1
                        else:
                            clean += 1
                            if clean >= CLEAR_N and jq_idx < len(JPEG_Q) - 1:
                                jq_idx += 1
                                clean = 0
                elif CODEC != 'chunk':
                    if congested:
                        clean = 0
                        GATE = min(GATE_MAX[CODEC], GATE + GATE_STEP[CODEC])
                    else:
                        clean += 1
                        if clean >= CLEAR_N:
                            GATE = max(GATE_MIN[CODEC], GATE - GATE_STEP[CODEC])
                            clean = 0

            t_d = time.ticks_ms()
            if time.ticks_diff(t_d, t_b) > STALE_MS:
                stale = True

            st_n += 1
            if packet is not None or jbytes is not None:
                st_f += 1
            st_cap += time.ticks_diff(t_b, t_a)
            st_enc += time.ticks_diff(t_c, t_b)
            st_snd += snd
            if dropped:
                st_drop += 1
            st_bytes += len(packet) if packet is not None else (len(jbytes) if jbytes is not None else 0)

            if time.ticks_diff(time.ticks_ms(), st_t) >= 3000:
                if not usb_on:      # keep the USB line clean while streaming over it
                    print('%s: %.1f fps | cap %d enc %d send %d ms | %d B/frame | drop %d | cli %d | level %d | rssi %d dBm' % (
                        current_name, st_f * 1000 / time.ticks_diff(time.ticks_ms(), st_t),
                        st_cap // st_n, st_enc // st_n, st_snd // st_n,
                        st_bytes // st_n, st_drop, len(streams), JPEG_Q[jq_idx] if CODEC == 'jpeg' else GATE,
                        rssi()))
                st_t = time.ticks_ms()
                st_n = 0
                st_f = 0
                st_cap = 0
                st_enc = 0
                st_snd = 0
                st_bytes = 0
                st_drop = 0

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print('loop error:', e)
            gc.collect()
            time.sleep_ms(20)

except KeyboardInterrupt:
    print('Stopped')
finally:
    close_streams()
    if srv is not None:
        try:
            srv.close()
        except OSError:
            pass
    try:
        cam.deinit()
    except Exception:
        pass
    restore_repl()