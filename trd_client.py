#!/usr/bin/env python3
"""TRD client: Text Remote Desktop for Monocell."""
import argparse
import gzip
import html
import json
import os
import random
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import functools

import numpy as np

from PyQt6 import QtCore, QtGui, QtWidgets

VERSION = 1
F_HELLO, F_FONT, F_META, F_CELLS, F_CURSOR, F_AUDIO, F_STATUS, F_ERROR = range(1, 9)
F_TEXT, F_GRAPHICS, F_IMAGE = 9, 10, 11
C_HELLO, C_KEY, C_MOUSE, C_PASTE, C_AUDIO, C_FULL, C_PING = range(10, 17)
C_SNAPSHOT = 18

MOUSE_KINDS = ('down', 'up', 'drag', 'move', 'wheelup', 'wheeldown')
DEFAULT_META = '~/.cache/monocell/trd.json'

def safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            return None
    return wrapper



def icon():
    """The TRD icon from the icon theme, else from the icon files next to us."""
    ic = QtGui.QIcon.fromTheme('trd')
    if not ic.isNull():
        return ic
    for base in (os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trd.png'),
                 os.path.expanduser('~/.local/share/icons/hicolor/256x256/apps/trd.png')):
        if os.path.exists(base):
            return QtGui.QIcon(base)
    return QtGui.QIcon.fromTheme('utilities-terminal')


def argb(rgb):
    r, g, b = rgb
    return 0xFF000000 | (int(r) << 16) | (int(g) << 8) | int(b)


def parse_psf(raw):
    data = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
    if data[:2] == b'\x36\x04':
        mode, charsize = data[2], data[3]
        glyphs = 512 if mode & 0x01 else 256
        off, width, height = 4, 8, charsize
    elif data[:4] == b'\x72\xb5\x4a\x86' and len(data) >= 32:
        _m, _v, headersize, _flags, glyphs, charsize, height, width = \
            struct.unpack('<IIIIIIII', data[:32])
        off = headersize
    else:
        raise ValueError('unbekanntes Fontformat')
    bpr = (width + 7) // 8
    need = height * bpr
    raw = np.frombuffer(data, np.uint8, count=glyphs * charsize, offset=off)
    raw = raw.reshape(glyphs, charsize)[:, :need]
    bits = np.unpackbits(raw, axis=1).reshape(glyphs, height, bpr * 8)
    return bits[:, :, :width].astype(bool), width, height



class Recorder:
    RATE = 44100
    FPS = 15
    FRAME_SAMPLES = RATE // FPS

    def __init__(self, path):
        self.path = path
        self.video_path = path + '.video.mkv'
        self.audio_path = path + '.wav'
        self.proc = None
        self.wav = None
        self.audio_lock = threading.Lock()
        self.frame_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.frame_image = None
        self.samples = 0
        self.frames = 0
        self.audio_bytes = 0
        self.started = 0.0
        self.error = ''
        self.logfile = path + '.log'
        self.log = None
        self.running = False
        self.pump_stop = None
        self.pump_thread = None

    def start(self):
        import wave
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise RuntimeError('ffmpeg nicht gefunden')
        cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '1024x768', '-r', str(self.FPS),
               '-i', 'pipe:0',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
               '-pix_fmt', 'yuv420p', '-f', 'matroska', self.video_path]
        self.log = open(self.logfile, 'wb')
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=self.log,
                                     start_new_session=True)
        self.wav = wave.open(self.audio_path, 'wb')
        self.wav.setnchannels(2)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.RATE)
        self.running = True
        self.started = time.monotonic()
        self.pump_stop = threading.Event()
        self.pump_thread = threading.Thread(target=self._pump, daemon=True)
        self.pump_thread.start()
        return self

    def set_frame(self, img):
        if not self.running:
            return
        with self.frame_lock:
            self.frame_image = img

    def _write_frame(self, img):
        img = img.convertToFormat(QtGui.QImage.Format.Format_RGB888)
        data = img.constBits().asstring(img.sizeInBytes())
        try:
            self.proc.stdin.write(data)
            self.frames += 1
        except (BrokenPipeError, OSError, ValueError, AttributeError):
            pass

    def audio(self, data):
        if not self.running:
            return
        with self.audio_lock:
            if self.wav is None:
                return
            try:
                self.wav.writeframesraw(data)
                self.audio_bytes += len(data)
            except (OSError, ValueError):
                return
        self.samples += len(data) // 4
        self._advance_frames()

    def _advance_frames(self):
        with self.state_lock:
            while self.samples >= self.frames * self.FRAME_SAMPLES:
                with self.frame_lock:
                    img = self.frame_image
                if img is None:
                    break
                self._write_frame(img)

    def _pump(self):
        rate = self.RATE
        step = max(1, rate // 20)
        while not self.pump_stop.wait(0.05):
            if not self.running:
                return
            target = int((time.monotonic() - self.started) * rate)
            with self.audio_lock:
                if not self.running or self.wav is None:
                    return
                deficit = target - self.samples
                if deficit > rate * 0.15:
                    while deficit > 0:
                        n = min(deficit, step)
                        try:
                            self.wav.writeframesraw(b'\0' * (n * 4))
                            self.audio_bytes += n * 4
                            self.samples += n
                        except (OSError, ValueError):
                            return
                        deficit -= n
            self._advance_frames()

    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0.0

    def stop(self):
        self.running = False
        if self.pump_stop:
            self.pump_stop.set()
        if self.pump_thread:
            self.pump_thread.join(timeout=2)
            self.pump_thread = None
        with self.audio_lock:
            wav, self.wav = self.wav, None
        if wav is not None:
            try:
                wav.close()
            except OSError:
                pass
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            if self.log:
                try:
                    self.log.close()
                    self.log = None
                except OSError:
                    pass
            self.proc = None
        ffmpeg = shutil.which('ffmpeg')
        if ffmpeg and os.path.exists(self.video_path) and os.path.exists(self.audio_path):
            mux = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                   '-i', self.video_path, '-i', self.audio_path,
                   '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k',
                   '-f', 'matroska', self.path]
            try:
                r = subprocess.run(mux, capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    self.error = (r.stderr or r.stdout).strip()[:400]
                else:
                    for p in (self.video_path, self.audio_path, self.logfile):
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
            except (OSError, subprocess.SubprocessError) as e:
                self.error = str(e)
        return self.path, self.error

class Screen:
    def __init__(self, meta, font_bytes):
        self.cols = 0
        self.rows = 0
        self.cw = 8
        self.ch = 16
        self.bits = None
        self.nglyphs = 0
        self.fg = []
        self.bg = []
        self.gfx = False
        self.cursor = None
        self.set_font(font_bytes)
        self.set_meta(meta)
        self.alloc()

    def set_font(self, font_bytes):
        self.font_bytes = font_bytes
        self.bits, self.cw, self.ch = parse_psf(font_bytes)
        self.nglyphs = self.bits.shape[0]

    def set_meta(self, meta):
        self.meta = meta
        self.cols = int(meta['cols'])
        self.rows = int(meta['rows'])
        self.fg = [argb(c) for c in meta['fg']]
        self.bg = [argb(c) for c in meta['bg']]
        self.fg_rgb = [tuple(c) for c in meta['fg']]
        self.bg_rgb = [tuple(c) for c in meta['bg']]
        self.gfx = bool(meta.get('gfx'))
        self.cursor = tuple(meta['cursor']) if meta.get('cursor') else None

    def alloc(self):
        self.w_px = self.cols * self.cw
        self.h_px = self.rows * self.ch
        self.g = np.zeros((self.rows, self.cols), np.uint16)
        self.f = np.zeros((self.rows, self.cols), np.uint8)
        self.b = np.zeros((self.rows, self.cols), np.uint8)
        self.pix = np.zeros((self.h_px, self.w_px), np.uint32)
        self.pix[:, :] = self.bg[0] if self.bg else 0xFF000000
        self._qimg = None
        self._banner = None

    def qimage(self):
        if self._qimg is None or self._qimg.width() != self.w_px or self._qimg.height() != self.h_px:
            self._qimg = QtGui.QImage(self.pix.data, self.w_px, self.h_px,
                                      self.pix.strides[0], QtGui.QImage.Format.Format_ARGB32)
        return self._qimg

    def _glyph(self, g):
        if g >= self.nglyphs:
            g = 63 if self.nglyphs > 63 else 0
        return g

    def draw_cell(self, x, y):
        if not (0 <= x < self.cols and 0 <= y < self.rows):
            return
        g = self._glyph(int(self.g[y, x]))
        mask = self.bits[g]
        fg = self.fg[int(self.f[y, x])]
        bg = self.bg[int(self.b[y, x])]
        if self.cursor == (x, y):
            fg, bg = bg, fg
        block = self.pix[y * self.ch:(y + 1) * self.ch, x * self.cw:(x + 1) * self.cw]
        block[mask] = fg
        block[~mask] = bg

    def set_cell(self, x, y, g, fg, bg):
        if not (0 <= x < self.cols and 0 <= y < self.rows):
            return
        self.g[y, x] = g
        self.f[y, x] = fg & 15
        self.b[y, x] = bg & 15
        self.draw_cell(x, y)

    def set_cursor(self, x, y):
        old = self.cursor
        if x is None or y is None or x < 0 or y < 0:
            self.cursor = None
        else:
            self.cursor = (x, y)
        if old and old != self.cursor:
            self.draw_cell(*old)
        if self.cursor:
            self.draw_cell(*self.cursor)

    def _text_image(self, lines, fg, bg):
        cols = max((len(line) for line in lines), default=1)
        rows = max(1, len(lines))
        arr = np.zeros((rows * self.ch, cols * self.cw), np.uint32)
        arr[:, :] = bg
        for j, line in enumerate(lines):
            for i, ch in enumerate(line):
                cp = ord(ch)
                g = cp if 32 <= cp < 127 else 63
                g = self._glyph(g)
                mask = self.bits[g]
                block = arr[j * self.ch:(j + 1) * self.ch, i * self.cw:(i + 1) * self.cw]
                block[mask] = fg
                block[~mask] = bg
        img = QtGui.QImage(arr.data, cols * self.cw, rows * self.ch,
                           arr.strides[0], QtGui.QImage.Format.Format_ARGB32)
        return img, arr

    def banner_image(self):
        if self._banner is not None:
            return self._banner
        fg = self.fg[14] if len(self.fg) > 14 else self.fg[-1]
        bg = self.bg[0] if self.bg else 0xFF000000
        lines = [
            'GRAFIKMODUS',
            '',
            'Bild wird geladen ...',
            '',
            'Audio laeuft weiter.',
        ]
        img, keep = self._text_image(lines, fg, bg)
        self._banner = (img, keep)
        return self._banner


class Connection(QtCore.QObject):
    hello = QtCore.pyqtSignal(dict)
    ready = QtCore.pyqtSignal(dict, bytes)
    cells = QtCore.pyqtSignal(bytes)
    cursor = QtCore.pyqtSignal(int, int)
    status = QtCore.pyqtSignal(bool)
    graphics = QtCore.pyqtSignal(bytes)
    image = QtCore.pyqtSignal(bytes)
    text_snapshot = QtCore.pyqtSignal(dict)
    problem = QtCore.pyqtSignal(str)
    closed = QtCore.pyqtSignal(str)

    def __init__(self, host, user, auth, password, meta_path, audio=True, debug=False, direct=False):
        super().__init__()
        self.host = host
        self.user = user
        self.auth = auth
        self.password = password
        self.meta_path = meta_path
        self.audio_enabled = audio
        self.debug = debug
        self.direct = direct
        self.ctl = os.path.join(os.path.expanduser('~/.cache/trd'),
                                'cm-%s' % abs(hash((host, user))))
        self.askpass = None
        self.secret = None
        self.sock = None
        self.buf = b''
        self.player = None
        self._audio_rest = b''
        self.player_lock = threading.Lock()
        self.recorder = None
        self.audio_bytes = 0
        self.frames = 0
        self.cell_count = 0
        self._stopped = False
        self._pending_meta = None
        os.makedirs(os.path.dirname(self.ctl), exist_ok=True)

    def start(self):
        thread = threading.Thread(target=self._setup, daemon=True)
        thread.start()

    def _ssh(self, extra, command=None, timeout=20):
        cmd = ['ssh', '-S', self.ctl, '-o', 'StrictHostKeyChecking=accept-new',
               '-o', 'ConnectTimeout=10'] + extra
        target = '%s@%s' % (self.user, self.host)
        if command is not None:
            cmd += [target] + command
        else:
            cmd += [target]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def _start_master(self, logpath):
        target = '%s@%s' % (self.user, self.host)
        opts = ['-o', 'ControlMaster=yes', '-o', 'ControlPath=' + self.ctl,
                '-o', 'ControlPersist=120', '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15']
        env = dict(os.environ)
        if self.auth == 'password':
            d = os.path.join(os.path.expanduser('~/.cache/trd'), 'ask-%d' % os.getpid())
            os.makedirs(d, exist_ok=True)
            self.secret = os.path.join(d, 'secret')
            self.askpass = os.path.join(d, 'askpass')
            fd = os.open(self.secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, (self.password + '\n').encode())
            os.close(fd)
            with open(self.askpass, 'w') as f:
                f.write('#!/bin/sh\ncat %s\n' % shlex.quote(self.secret))
            os.chmod(self.askpass, 0o700)
            opts += ['-o', 'PubkeyAuthentication=no',
                     '-o', 'PreferredAuthentications=password,keyboard-interactive',
                     '-o', 'NumberOfPasswordPrompts=1']
            env['SSH_ASKPASS'] = self.askpass
            env['SSH_ASKPASS_REQUIRE'] = 'force'
            env['DISPLAY'] = env.get('DISPLAY') or ':0'
        else:
            opts += ['-o', 'BatchMode=yes', '-o', 'PreferredAuthentications=publickey']
        cmd = ['ssh', '-fN'] + opts + [target]
        with open(logpath, 'wb') as log:
            proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=log, start_new_session=True,
                                  timeout=30)
        return proc.returncode

    def _read_meta(self):
        paths = [self.meta_path or DEFAULT_META]
        if paths[0].strip() not in ('', DEFAULT_META):
            paths.append(DEFAULT_META)
        detail = ''
        for path in paths:
            remote = path if path.startswith('~') else shlex.quote(path)
            r = self._ssh([], ['cat', '--', remote], timeout=15)
            if r.returncode != 0:
                detail = (r.stderr or r.stdout).strip()
                continue
            try:
                return json.loads(r.stdout)
            except ValueError:
                detail = 'ungueltiger Inhalt: ' + r.stdout.strip()[:300]
                continue
        if 'No such file' in detail or 'not found' in detail or 'not readable' in detail or not detail:
            raise RuntimeError('TRD-Server nicht aktiv. Bitte Monocell auf dem Zielgeraet '
                               'einmal beenden und neu starten (abmelden/anmelden).\n' + detail)
        raise RuntimeError('TRD-Metadatei nicht lesbar:\n' + detail)

    def _add_forward(self, remote_port):
        target = '%s@%s' % (self.user, self.host)
        for _ in range(12):
            local = random.randint(42000, 60000)
            cmd = ['ssh', '-S', self.ctl, '-o', 'StrictHostKeyChecking=accept-new',
                   '-O', 'forward', '-L', '127.0.0.1:%d:127.0.0.1:%d' % (local, remote_port),
                   target]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                               stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                return local
        raise RuntimeError('SSH-Weiterleitung fehlgeschlagen:\n' + r.stderr.strip())

    def _setup(self):
        logpath = os.path.join(os.path.expanduser('~/.cache/trd'), 'ssh-%d.log' % os.getpid())
        try:
            if self.direct:
                path = os.path.expanduser(self.meta_path or '~/.cache/monocell/trd.json')
                with open(path, 'r') as f:
                    meta = json.load(f)
                sock = socket.create_connection(('127.0.0.1', int(meta['port'])), timeout=8)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock = sock
                self._send(C_HELLO, str(meta['token']).encode())
                self._send(C_AUDIO, b'\x01')
                self.hello.emit(meta)
                self._reader()
                return
            rc = self._start_master(logpath)
            for _ in range(50):
                check = self._ssh(['-O', 'check'], timeout=8)
                if check.returncode == 0:
                    break
                time.sleep(0.2)
            else:
                detail = ''
                try:
                    with open(logpath, 'r', errors='replace') as f:
                        detail = f.read()[-600:]
                except OSError:
                    pass
                if rc != 0 or 'Permission denied' in detail or 'denied' in detail.lower():
                    raise RuntimeError('SSH-Anmeldung fehlgeschlagen.\n' + detail.strip())
                raise RuntimeError('SSH-Verbindung nicht zustande gekommen.\n' + detail.strip())
            self._cleanup_secret()
            meta = self._read_meta()
            if 'port' not in meta or 'token' not in meta:
                raise RuntimeError('TRD-Metadatei ohne Port/Token.')
            port = self._add_forward(int(meta['port']))
            sock = socket.create_connection(('127.0.0.1', port), timeout=8)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = sock
            self._send(C_HELLO, str(meta['token']).encode())
            self._send(C_AUDIO, b'\x01')
            self.hello.emit(meta)
            self._reader()
        except Exception as e:
            if not self._stopped:
                self.problem.emit(str(e))
        finally:
            self._cleanup_secret()

    def _cleanup_secret(self):
        self.password = ''
        for p in (self.secret, self.askpass):
            try:
                if p:
                    os.unlink(p)
            except OSError:
                pass
        self.secret = self.askpass = None

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError('Verbindung beendet')
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _recv_frame(self):
        head = self._recv_exact(5)
        kind, length = struct.unpack('<BI', head)
        return kind, (self._recv_exact(length) if length else b'')

    def _reader(self):
        try:
            while not self._stopped:
                kind, payload = self._recv_frame()
                if kind in (F_HELLO,):
                    self._pending_meta = json.loads(payload)
                elif kind == F_FONT:
                    meta = self._pending_meta or {}
                    self._pending_meta = None
                    self.ready.emit(meta, payload)
                elif kind == F_META:
                    self._pending_meta = json.loads(payload)
                elif kind == F_CELLS:
                    self.frames += 1
                    self.cells.emit(payload)
                elif kind == F_CURSOR:
                    x, y = struct.unpack('<hh', payload[:4])
                    self.cursor.emit(x, y)
                elif kind == F_STATUS:
                    self.status.emit(bool(payload and payload[0]))
                elif kind == F_AUDIO:
                    self._play_audio(payload)
                elif kind == F_TEXT:
                    self.text_snapshot.emit(json.loads(payload))
                elif kind == F_GRAPHICS:
                    self.graphics.emit(payload)
                elif kind == F_IMAGE:
                    self.image.emit(payload)
                elif kind == F_ERROR:
                    raise RuntimeError('TRD-Server: ' + payload.decode('utf-8', 'replace'))
        except (EOFError, OSError, ValueError) as e:
            if not self._stopped:
                self.closed.emit(str(e))
        finally:
            self._stop_player()

    def _send(self, kind, payload):
        if not self.sock:
            return
        try:
            self.sock.sendall(struct.pack('<BI', kind, len(payload)) + payload)
        except OSError:
            pass

    def send_key(self, name):
        self._send(C_KEY, name.encode('utf-8'))

    def send_mouse(self, kind, x, y, button=0, mods=0, clicks=1):
        self._send(C_MOUSE, struct.pack('<BffBBH', kind, x, y, button, mods, clicks))

    def send_paste(self, text):
        self._send(C_PASTE, text.encode('utf-8'))

    def set_audio(self, on):
        self.audio_enabled = bool(on)
        if not on:
            self._stop_player()

    def request_full(self):
        self._send(C_FULL, b'')

    def send_snapshot(self):
        self._send(C_SNAPSHOT, b'')

    def _start_player(self):
        if self.player or not self.audio_enabled:
            return
        cmds = []
        if shutil.which('pacat'):
            cmds.append(['pacat', '--playback', '--raw', '--format=s16le',
                         '--rate=44100', '--channels=2', '--latency-msec=400'])
        if shutil.which('pw-play'):
            cmds.append(['pw-play', '--raw', '--format=s16le', '--rate=44100',
                         '--channels=2', '-'])
        if shutil.which('aplay'):
            cmds.append(['aplay', '-q', '-t', 'raw', '-f', 'S16_LE', '-r', '44100', '-c', '2'])
        for cmd in cmds:
            try:
                self.player = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                               stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL,
                                               start_new_session=True)
                return
            except OSError:
                continue

    def _stop_player(self):
        with self.player_lock:
            p, self.player = self.player, None
        if p:
            try:
                p.stdin.close()
            except (OSError, AttributeError):
                pass
            try:
                p.terminate()
            except OSError:
                pass

    def _play_audio(self, payload):
        dump = os.environ.get('TRD_DUMP_AUDIO')
        if dump:
            with open(dump, 'ab') as f:
                f.write(payload)
        if self.recorder:
            self.recorder.audio(payload)
        if not self.audio_enabled:
            return
        # whole s16le stereo frames only: a payload with an odd byte count would
        # shift every following sample by one byte - the player turns that into
        # static. An older server can send such chunks, so the rest is carried.
        data = self._audio_rest + payload
        n = len(data) - (len(data) % 4)
        self._audio_rest = data[n:]
        if not n:
            return
        data = data[:n]
        with self.player_lock:
            if not self.player:
                self._start_player()
            p = self.player
        if not p:
            return
        try:
            p.stdin.write(data)
            p.stdin.flush()
            self.audio_bytes += len(data)
        except (BrokenPipeError, OSError):
            self._stop_player()

    def close(self):
        self._stopped = True
        self._stop_player()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        try:
            self._ssh(['-O', 'exit'], timeout=5)
        except Exception:
            pass
        self._cleanup_secret()


class View(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.screen = None
        self.rect_img = QtCore.QRect()
        self.scale = 1.0
        self.gfx_image = None
        self.gfx_size = None
        self.toast_text = ''
        self.toast_until = 0.0
        self._toast_timer = None
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self._buttons = 0
        self._last_cell = None

    def set_screen(self, screen):
        self.screen = screen
        self.updateGeometry()
        self.update()

    def image_size(self):
        if not self.screen:
            return 800, 600
        if self.screen.gfx and self.gfx_size:
            return self.gfx_size.width(), self.gfx_size.height()
        return self.screen.w_px, self.screen.h_px

    def natural_size(self):
        iw, ih = self.image_size()
        return QtCore.QSize(int(iw * self.scale), int(ih * self.scale))

    def sizeHint(self):
        return self.natural_size()

    def minimumSizeHint(self):
        return QtCore.QSize(240, 160)

    def _fit(self):
        if not self.screen:
            return QtCore.QRect()
        w, h = self.width(), self.height()
        if w <= 2 or h <= 2:
            return QtCore.QRect()
        sw, sh = self.image_size()
        s = min(w / float(sw), h / float(sh))
        iw = max(1, int(sw * s))
        ih = max(1, int(sh * s))
        return QtCore.QRect((w - iw) // 2, (h - ih) // 2, iw, ih)

    @safe
    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(12, 12, 12))
        if not self.screen:
            p.setPen(QtGui.QColor(220, 220, 220))
            f = p.font()
            f.setPointSize(14)
            p.setFont(f)
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, 'Verbinde ...')
            return
        rect = self._fit()
        self.rect_img = rect
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, False)
        if self.screen.gfx:
            if self.gfx_image is not None and not self.gfx_image.isNull():
                p.drawImage(rect, self.gfx_image)
            else:
                p.fillRect(rect, QtGui.QColor(*self.screen.bg_rgb[0]) if self.screen.bg_rgb else QtGui.QColor(0, 0, 0))
                img, keep = self.screen.banner_image()
                self._banner_keep = keep
                s = min(rect.width() / float(img.width()), rect.height() / float(img.height())) * 0.9
                w = max(1, int(img.width() * s))
                h = max(1, int(img.height() * s))
                p.drawImage(QtCore.QRect(rect.center().x() - w // 2, rect.center().y() - h // 2, w, h), img)
        else:
            p.drawImage(rect, self.screen.qimage())
        now = time.monotonic()
        if self.toast_text and now < self.toast_until:
            f = p.font()
            f.setPointSize(10)
            p.setFont(f)
            fm = p.fontMetrics()
            text = fm.elidedText(self.toast_text, QtCore.Qt.TextElideMode.ElideMiddle,
                                 max(100, rect.width() - 40))
            tw = fm.horizontalAdvance(text) + 16
            th = fm.height() + 10
            box = QtCore.QRect(rect.x() + 8, rect.y() + 8, min(tw, rect.width() - 16), th)
            p.fillRect(box, QtGui.QColor(0, 0, 0, 195))
            p.setPen(QtGui.QColor(240, 240, 240))
            p.drawText(box.adjusted(8, 0, -4, 0),
                       QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft, text)
        if getattr(self.window_ref, 'recording', False) and getattr(self.window_ref, 'recorder', None):
            secs = int(self.window_ref.recorder.elapsed())
            label = 'REC %02d:%02d' % (secs // 60, secs % 60)
            f = p.font()
            f.setPointSize(9)
            f.setBold(True)
            p.setFont(f)
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label)
            x = rect.right() - tw - 24
            y = rect.top() + 10
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(230, 40, 40))
            p.drawEllipse(QtCore.QRect(x - 14, y + 3, 9, 9))
            p.setPen(QtGui.QColor(240, 240, 240))
            p.drawText(x, y + fm.ascent(), label)

    def _cell(self, pos):
        if not self.screen or not self.rect_img.isValid() or self.rect_img.isEmpty():
            return None
        if not QtCore.QRectF(self.rect_img).contains(pos):
            return None
        x = (pos.x() - self.rect_img.x()) / float(self.rect_img.width()) * self.screen.cols
        y = (pos.y() - self.rect_img.y()) / float(self.rect_img.height()) * self.screen.rows
        if x < 0 or y < 0 or x >= self.screen.cols or y >= self.screen.rows:
            return None
        return x, y

    def toast(self, text, ms=3500):
        self.toast_text = text
        self.toast_until = time.monotonic() + ms / 1000.0
        self.update()
        if self._toast_timer:
            self._toast_timer.stop()
        self._toast_timer = QtCore.QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._clear_toast)
        self._toast_timer.start(ms)

    def _clear_toast(self):
        self.toast_text = ''
        self.update()

    def _send_mouse(self, kind, pos, button=0):
        c = self._cell(pos)
        if c is None:
            return
        x, y = c
        if not (self.screen and self.screen.gfx):
            x, y = int(x), int(y)
        mods = 0
        m = QtWidgets.QApplication.keyboardModifiers()
        if m & QtCore.Qt.KeyboardModifier.ShiftModifier:
            mods |= 1
        if m & QtCore.Qt.KeyboardModifier.AltModifier:
            mods |= 2
        if m & QtCore.Qt.KeyboardModifier.ControlModifier:
            mods |= 4
        self.window_ref.connection.send_mouse(MOUSE_KINDS.index(kind), x, y, button, mods, 1)
        self._last_cell = (x, y)

    @safe
    def mousePressEvent(self, ev):
        self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        self._buttons = 0
        btn = ev.button()
        if btn == QtCore.Qt.MouseButton.LeftButton:
            self._buttons = 1
            self._send_mouse('down', ev.position(), 0)
        elif btn == QtCore.Qt.MouseButton.MiddleButton:
            self._buttons = 2
            self._send_mouse('down', ev.position(), 1)
        elif btn == QtCore.Qt.MouseButton.RightButton:
            self._buttons = 4
            self._send_mouse('down', ev.position(), 2)

    @safe
    def mouseReleaseEvent(self, ev):
        btn = ev.button()
        if btn == QtCore.Qt.MouseButton.LeftButton:
            self._send_mouse('up', ev.position(), 0)
        elif btn == QtCore.Qt.MouseButton.MiddleButton:
            self._send_mouse('up', ev.position(), 1)
        elif btn == QtCore.Qt.MouseButton.RightButton:
            self._send_mouse('up', ev.position(), 2)
        self._buttons = 0

    @safe
    def mouseMoveEvent(self, ev):
        if self._buttons:
            self._send_mouse('drag', ev.position(), int(self._buttons).bit_length() - 1)
        else:
            self._send_mouse('move', ev.position(), 0)

    @safe
    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy > 0:
            self._send_mouse('wheelup', ev.position(), 0)
        elif dy < 0:
            self._send_mouse('wheeldown', ev.position(), 0)

    @safe
    def keyPressEvent(self, ev):
        if self.window_ref.handle_local_key(ev):
            return
        res = key_name(ev)
        if not res:
            return
        mode, value = res
        if mode == 'paste':
            self.window_ref.connection.send_paste(value)
        else:
            self.window_ref.connection.send_key(value)

    @safe
    def focusOutEvent(self, ev):
        self._buttons = 0
        super().focusOutEvent(ev)


SPECIAL = {
    QtCore.Qt.Key.Key_Escape: 'esc',
    QtCore.Qt.Key.Key_Return: 'enter',
    QtCore.Qt.Key.Key_Enter: 'enter',
    QtCore.Qt.Key.Key_Tab: 'tab',
    QtCore.Qt.Key.Key_Backspace: 'backspace',
    QtCore.Qt.Key.Key_Delete: 'delete',
    QtCore.Qt.Key.Key_Insert: 'insert',
    QtCore.Qt.Key.Key_Home: 'home',
    QtCore.Qt.Key.Key_End: 'end',
    QtCore.Qt.Key.Key_PageUp: 'pgup',
    QtCore.Qt.Key.Key_PageDown: 'pgdn',
    QtCore.Qt.Key.Key_Up: 'up',
    QtCore.Qt.Key.Key_Down: 'down',
    QtCore.Qt.Key.Key_Left: 'left',
    QtCore.Qt.Key.Key_Right: 'right',
    QtCore.Qt.Key.Key_F1: 'f1', QtCore.Qt.Key.Key_F2: 'f2', QtCore.Qt.Key.Key_F3: 'f3',
    QtCore.Qt.Key.Key_F4: 'f4', QtCore.Qt.Key.Key_F5: 'f5', QtCore.Qt.Key.Key_F6: 'f6',
    QtCore.Qt.Key.Key_F7: 'f7', QtCore.Qt.Key.Key_F8: 'f8', QtCore.Qt.Key.Key_F9: 'f9',
    QtCore.Qt.Key.Key_F10: 'f10', QtCore.Qt.Key.Key_F11: 'f11', QtCore.Qt.Key.Key_F12: 'f12',
}

IGNORE = {
    QtCore.Qt.Key.Key_Shift, QtCore.Qt.Key.Key_Control, QtCore.Qt.Key.Key_Alt,
    QtCore.Qt.Key.Key_Meta, QtCore.Qt.Key.Key_AltGr, QtCore.Qt.Key.Key_CapsLock,
    QtCore.Qt.Key.Key_NumLock, QtCore.Qt.Key.Key_ScrollLock, QtCore.Qt.Key.Key_Menu,
}


def key_name(ev):
    key = ev.key()
    mods = ev.modifiers()
    ctrl = bool(mods & QtCore.Qt.KeyboardModifier.ControlModifier)
    alt = bool(mods & QtCore.Qt.KeyboardModifier.AltModifier)
    shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)
    if key in IGNORE:
        return None
    prefix = ('ctrl+' if ctrl else '') + ('alt+' if alt else '') + ('shift+' if shift else '')
    if key in SPECIAL:
        return ('key', prefix + SPECIAL[key])
    text = ev.text()
    if not text:
        return None
    if len(text) > 1:
        return ('paste', text)
    o = ord(text[0])
    if o < 32 or o == 127:
        if o == 9:
            return ('key', prefix + 'tab')
        if o in (10, 13):
            return ('key', prefix + 'enter')
        if o in (8, 127):
            return ('key', prefix + 'backspace')
        if o == 27:
            return ('key', prefix + 'esc')
        if o == 0:
            return ('key', prefix + 'space')
        if 1 <= o <= 26:
            base = chr(96 + o)
            p = 'ctrl+' + ('alt+' if alt else '') + ('shift+' if shift else '')
            return ('key', p + base)
        return None
    if ctrl and alt:
        return ('key', text)
    if ctrl:
        return ('key', 'ctrl+' + text)
    if alt:
        return ('key', 'alt+' + text)
    return ('key', text)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, connection, opts):
        super().__init__()
        self.connection = connection
        self.opts = opts
        self.scr = None
        self.scale = 1.0
        self._closing = False
        self.view = View(self)
        self.setCentralWidget(self.view)
        self.setWindowIcon(icon())
        self.status_text = ''
        self.gfx = False
        self.gfx_image = None
        self.recording = False
        self.recorder = None
        self.rec_timer = None
        self.auto_size = True
        self._program_resize = False
        self._headless_sent = False
        self._build_menu()
        self.resize(1100, 800)
        connection.ready.connect(self.on_ready)
        connection.cells.connect(self.on_cells)
        connection.cursor.connect(self.on_cursor)
        connection.status.connect(self.on_status)
        connection.graphics.connect(self.on_graphics)
        connection.image.connect(self.on_image)
        connection.text_snapshot.connect(self.on_text_snapshot)
        connection.problem.connect(self.on_problem)
        connection.closed.connect(self.on_closed)
        connection.hello.connect(lambda meta: self._title(meta))

    def _build_menu(self):
        bar = self.menuBar()
        m = bar.addMenu('Verbindung')
        a = QtGui.QAction('Trennen', self)
        a.triggered.connect(self.close)
        m.addAction(a)
        a = QtGui.QAction('Beenden', self)
        a.setShortcut(QtGui.QKeySequence('Ctrl+Shift+Q'))
        a.triggered.connect(self.close)
        m.addAction(a)
        m = bar.addMenu('Bearbeiten')
        a = QtGui.QAction('Einfuegen (Workstation -> Ziel)', self)
        a.setShortcut(QtGui.QKeySequence('Ctrl+Shift+V'))
        a.triggered.connect(self.paste_local)
        m.addAction(a)
        m = bar.addMenu('Ansicht')
        for name, scale in (('1:1', 1.0), ('1,5x', 1.5), ('2x', 2.0)):
            a = QtGui.QAction('Zoom ' + name, self)
            a.triggered.connect(lambda checked=False, s=scale: self.set_zoom(s))
            m.addAction(a)
        a = QtGui.QAction('Vollbild', self)
        a.setShortcut(QtGui.QKeySequence('F11'))
        a.triggered.connect(self.toggle_fullscreen)
        m.addAction(a)
        m = bar.addMenu('Aufnahme')
        a = QtGui.QAction('Screenshot speichern', self)
        a.setShortcut(QtGui.QKeySequence('Ctrl+Shift+S'))
        a.triggered.connect(self.take_screenshot)
        m.addAction(a)
        a = QtGui.QAction('Videoaufzeichnung starten/stoppen', self)
        a.setShortcut(QtGui.QKeySequence('Ctrl+Shift+R'))
        a.triggered.connect(self.toggle_recording)
        m.addAction(a)
        m = bar.addMenu('Audio')
        a = QtGui.QAction('Ton an/aus', self)
        a.setShortcut(QtGui.QKeySequence('Ctrl+M'))
        a.triggered.connect(self.toggle_audio)
        m.addAction(a)
        m = bar.addMenu('Hilfe')
        a = QtGui.QAction('Tasten', self)
        a.triggered.connect(self.show_help)
        m.addAction(a)

    @safe
    def _title(self, meta):
        title = meta.get('title') or self.connection.host
        self.base_title = 'TRD - %s - %sx%s' % (title, meta.get('cols', '?'), meta.get('rows', '?'))
        self.setWindowTitle(self.base_title)

    @safe
    def on_ready(self, meta, font_bytes):
        first = self.scr is None
        self.scr = Screen(meta, font_bytes)
        old_scale = self.scale
        if first:
            self.scale = self.opts['scale'] or self._default_scale()
        else:
            self.scale = old_scale
        self.view.scale = self.scale
        self.view.set_screen(self.scr)
        self.gfx = bool(meta.get('gfx'))
        self._title(meta)
        self.view.updateGeometry()
        if first:
            self.auto_size = True
            self._resize_natural()
        elif self.auto_size:
            self._resize_natural()
        self.view.update()
        if first and self.opts['headless']:
            QtCore.QTimer.singleShot(int(self.opts['seconds'] * 1000), self._finish_headless)
        if not self._headless_sent and self.opts['send_keys']:
            self._headless_sent = True
            QtCore.QTimer.singleShot(400, self._send_test_keys)
        if self.opts.get('click'):
            try:
                cx, cy = [int(v) for v in self.opts['click'].split(',')[:2]]
            except ValueError:
                cx = cy = None
            if cx is not None:
                def click():
                    self.connection.send_mouse(0, cx, cy, 0, 0, 1)
                    QtCore.QTimer.singleShot(90, lambda: self.connection.send_mouse(1, cx, cy, 0, 0, 1))
                QtCore.QTimer.singleShot(700, click)
        if first and self.opts.get('record'):
            self.start_recording(self.opts['record'])
        if first and self.opts.get('snapshot'):
            QtCore.QTimer.singleShot(900, self.take_screenshot)

    def _default_scale(self):
        if self.opts['headless']:
            return 1.0
        avail = self.screen().availableGeometry() if self.screen() else QtCore.QRect(0, 0, 1920, 1080)
        w = self.scr.w_px
        h = self.scr.h_px
        s = min((avail.width() - 40) / float(w), (avail.height() - 100) / float(h))
        s = min(s, 1.5)
        s = max(0.5, round(s * 4) / 4.0)
        if 1.0 < s < 1.25:
            s = 1.0
        return s

    def set_zoom(self, scale):
        if not self.scr:
            return
        self.auto_size = True
        self.scale = scale
        self.view.scale = scale
        self.view.updateGeometry()
        self._resize_natural()
        self.view.update()

    def _resize_natural(self):
        if not self.scr or self.isFullScreen() or self.isMaximized():
            return
        self._program_resize = True
        try:
            self.resize(self.view.natural_size() + QtCore.QSize(0, self.menuBar().sizeHint().height()))
        finally:
            self._program_resize = False

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if not self._program_resize and self.isVisible():
            self.auto_size = False

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def toggle_audio(self):
        self.connection.set_audio(not self.connection.audio_enabled)

    def paste_local(self):
        text = QtWidgets.QApplication.clipboard().text()
        if not text:
            self.view.toast('Zwischenablage ist leer')
            return
        text = text[:100000]
        self.connection.send_paste(text)
        self.view.toast('Eingefuegt: %d Zeichen' % len(text))

    def show_help(self):
        QtWidgets.QMessageBox.information(
            self, 'TRD - Tasten',
            'F11  Vollbild\nCtrl+M  Ton an/aus (lokal)\nCtrl+Shift+V  Einfuegen\n'
            'Ctrl+Shift+S  Screenshot (Text oder Grafik)\n'
            'Ctrl+Shift+R  Videoaufzeichnung mit Ton\n'
            'Ctrl+Shift+Q  Beenden\nCtrl+Plus / Ctrl+Minus  Zoom\n\n'
            'Maus: Klick, Ziehen, Rad. Tastatur wird 1:1 an das Zielgeraet gesendet.')

    def handle_local_key(self, ev):
        key = ev.key()
        mods = ev.modifiers()
        ctrl = bool(mods & QtCore.Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)
        if key == QtCore.Qt.Key.Key_F11:
            if ctrl:
                self.connection.send_key('f11')
            else:
                self.toggle_fullscreen()
            return True
        if ctrl and shift and key == QtCore.Qt.Key.Key_Q:
            self.close()
            return True
        if ctrl and shift and key == QtCore.Qt.Key.Key_V:
            self.paste_local()
            return True
        if ctrl and key == QtCore.Qt.Key.Key_M:
            self.toggle_audio()
            return True
        if ctrl and key in (QtCore.Qt.Key.Key_Plus, QtCore.Qt.Key.Key_Equal):
            self.set_zoom(min(3.0, self.scale + 0.25))
            return True
        if ctrl and key == QtCore.Qt.Key.Key_Minus:
            self.set_zoom(max(0.5, self.scale - 0.25))
            return True
        if ctrl and key == QtCore.Qt.Key.Key_0:
            self.set_zoom(1.0)
            return True
        return False

    @safe
    def on_cells(self, payload):
        if not self.scr:
            return
        if len(payload) < 4:
            return
        count = struct.unpack_from('<I', payload, 0)[0]
        off = 4
        for _ in range(count):
            if off + 8 > len(payload):
                break
            x, y, g, fg, bg = struct.unpack_from('<HHHBB', payload, off)
            off += 8
            self.scr.set_cell(x, y, g, fg, bg)
        self.connection.cell_count += count
        self.view.update()

    @safe
    def on_cursor(self, x, y):
        if not self.scr:
            return
        self.scr.set_cursor(x, y)
        self.view.update()

    @safe
    def on_status(self, gfx):
        self.gfx = gfx
        if self.scr:
            self.scr.gfx = gfx
        base = getattr(self, 'base_title', 'TRD')
        self.setWindowTitle(base + (' - GRAFIKMODUS' if gfx else ''))
        self.view.updateGeometry()
        if self.auto_size:
            self._resize_natural()
        self.view.update()

    @safe
    def on_problem(self, msg):
        self._closing = True
        if self.opts['headless']:
            print('ERROR:', msg, file=sys.stderr)
            QtWidgets.QApplication.exit(2)
            return
        QtWidgets.QMessageBox.critical(self, 'TRD - Verbindung', msg)
        self.close()

    @safe
    def on_closed(self, msg):
        if self._closing:
            return
        if self.opts['headless']:
            print('CLOSED:', msg, file=sys.stderr)
            QtWidgets.QApplication.exit(2 if self.connection.frames == 0 else 0)
        else:
            QtWidgets.QMessageBox.warning(self, 'TRD - Verbindung', 'Verbindung beendet: ' + msg)
            self.close()

    def _send_test_keys(self):
        keys = [k.strip() for k in self.opts['send_keys'].split(',') if k.strip()]
        for i, k in enumerate(keys):
            QtCore.QTimer.singleShot(i * 450, lambda kk=k: self.connection.send_key(kk))

    def _finish_headless(self):
        if self.recording:
            path, err = self.stop_recording()
            print('VIDEO', path, 'ERROR' if err else 'OK')
        self._save_shot()
        if self.scr:
            m = self.scr.meta
            print('META', json.dumps({'cols': m.get('cols'), 'rows': m.get('rows'),
                                      'font': m.get('font'), 'mode': m.get('mode'),
                                      'title': m.get('title')}))
        print('FRAMES', self.connection.frames, 'CELLS', self.connection.cell_count,
              'AUDIO', self.connection.audio_bytes)
        QtWidgets.QApplication.exit(0)


    @safe
    def on_image(self, payload):
        if len(payload) < 4:
            return
        img = QtGui.QImage.fromData(payload[4:])
        if img.isNull():
            return
        path = self._save_png(img)
        if path:
            self.view.toast('Screenshot: ' + path)

    @safe
    def on_graphics(self, payload):
        if len(payload) < 26:
            return
        W, H, cw, ch, x0, y0, n, bpp, stride = struct.unpack_from('<HHHHHHBBI', payload, 0)
        jlen, olen = struct.unpack_from('<II', payload, 18)
        if n <= 0 or cw <= 0 or ch <= 0 or len(payload) < 26 + jlen + olen:
            return
        jpeg = payload[26:26 + jlen]
        osd = payload[26 + jlen:26 + jlen + olen]
        img = QtGui.QImage.fromData(jpeg)
        if img.isNull():
            return
        uw, uh = cw * n, ch * n
        content = img.scaled(uw, uh, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                             QtCore.Qt.TransformationMode.FastTransformation)
        content = content.convertToFormat(QtGui.QImage.Format.Format_RGB888)
        cbuf = content.constBits().asstring(content.sizeInBytes())
        carr = np.frombuffer(cbuf, np.uint8).reshape(content.height(), content.bytesPerLine())
        carr = carr[:, :uw * 3].reshape(uh, uw, 3)
        arr = np.zeros((H, W, 3), np.uint8)
        x0 = max(0, min(W, x0))
        y0 = max(0, min(H, y0))
        ww = min(uw, W - x0)
        hh = min(uh, H - y0)
        if ww > 0 and hh > 0:
            arr[y0:y0 + hh, x0:x0 + ww] = carr[:hh, :ww]
        if olen >= 4 and bpp >= 3 and stride >= bpp:
            count = struct.unpack_from('<I', osd, 0)[0]
            o = 4
            for _ in range(count):
                if o + 8 > len(osd):
                    break
                off, ln = struct.unpack_from('<II', osd, o)
                o += 8
                data = osd[o:o + ln]
                o += ln
                for k in range(0, len(data) - bpp + 1, bpp):
                    idx = off + k
                    y = idx // stride
                    xb = idx % stride
                    x = xb // bpp
                    if 0 <= y < H and 0 <= x < W:
                        arr[y, x, 0] = data[k + 2]
                        arr[y, x, 1] = data[k + 1]
                        arr[y, x, 2] = data[k]
        qimg = QtGui.QImage(arr.tobytes(), W, H, W * 3,
                            QtGui.QImage.Format.Format_RGB888).copy()
        self.gfx_image = qimg
        self.view.gfx_image = qimg
        old = self.view.gfx_size
        self.view.gfx_size = QtCore.QSize(W, H)
        self.view.updateGeometry()
        if old != self.view.gfx_size and self.auto_size:
            self._resize_natural()
        self.view.update()

    @safe
    def on_text_snapshot(self, data):
        lines = data.get('lines') or []
        if not lines:
            return
        base = self._shot_base('.txt')
        txt = os.path.join(os.path.dirname(base), os.path.basename(base)[:-4])
        try:
            with open(base, 'w', encoding='utf-8') as f:
                f.write('\n'.join(line.rstrip() for line in lines) + '\n')
            with open(txt + '-colour.html', 'w', encoding='utf-8') as f:
                f.write(self._text_html(lines))
        except OSError as e:
            self.view.toast('Screenshot fehlgeschlagen: %s' % e)
            return
        self.view.toast('Screenshot: ' + base)

    def _text_html(self, lines):
        scr = self.scr
        cols = scr.cols if scr else max((len(l) for l in lines), default=0)
        rows = len(lines)
        out = ['<!doctype html><meta charset="utf-8"><title>TRD text screenshot</title>',
               '<body style="background:#000;color:#fff;margin:0">'
               '<pre style="font-family:monospace;font-size:16px;line-height:1.0;margin:16px">']
        for y in range(rows):
            line = lines[y] if y < len(lines) else ''
            for x in range(max(cols, len(line))):
                ch = line[x] if x < len(line) else ' '
                if scr is not None and y < scr.rows and x < scr.cols:
                    fg = scr.fg_rgb[int(scr.f[y, x])]
                    bg = scr.bg_rgb[int(scr.b[y, x])]
                else:
                    fg, bg = (255, 255, 255), (0, 0, 0)
                out.append('<span style="color:#%02x%02x%02x;background:#%02x%02x%02x">%s</span>'
                           % (fg[0], fg[1], fg[2], bg[0], bg[1], bg[2], html.escape(ch)))
            out.append('\n')
        out.append('</pre></body>')
        return ''.join(out)

    def _slug(self):
        host = getattr(self.connection, 'host', 'ziel') or 'ziel'
        return ''.join(c if c.isalnum() or c in '._-' else '_' for c in host)

    def _shot_base(self, ext):
        d = os.path.expanduser('~/trd-shots')
        os.makedirs(d, exist_ok=True)
        stem = os.path.join(d, 'trd-%s-%s' % (self._slug(), time.strftime('%Y%m%d-%H%M%S')))
        path = stem + ext
        n = 2
        while os.path.exists(path):
            path = '%s-%d%s' % (stem, n, ext)
            n += 1
        return path

    def _save_png(self, img):
        path = self._shot_base('.png')
        if img.save(path, 'PNG'):
            return path
        return None

    def take_screenshot(self):
        if not self.connection:
            return
        self.connection.send_snapshot()
        self.view.toast('Screenshot wird erstellt ...')

    def toggle_recording(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self, path=None):
        if self.recording:
            return
        if not path:
            d = os.path.expanduser('~/trd-videos')
            os.makedirs(d, exist_ok=True)
            stem = os.path.join(d, 'trd-%s-%s' % (self._slug(), time.strftime('%Y%m%d-%H%M%S')))
            path = stem + '.mkv'
            n = 2
            while os.path.exists(path):
                path = '%s-%d.mkv' % (stem, n)
                n += 1
        try:
            rec = Recorder(path).start()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, 'TRD - Aufnahme', str(e))
            return
        self.recorder = rec
        self.connection.recorder = rec
        self.recording = True
        self._record_frame()
        self.rec_timer = QtCore.QTimer(self)
        self.rec_timer.timeout.connect(self._record_frame)
        self.rec_timer.start(33)
        self.view.toast('Aufnahme: ' + path)

    def stop_recording(self):
        if not self.recording:
            return (None, None)
        if self.rec_timer:
            self.rec_timer.stop()
            self.rec_timer = None
        self.connection.recorder = None
        self.recording = False
        rec, self.recorder = self.recorder, None
        path, err = rec.stop()
        if err:
            self.view.toast('Video-Fehler: ' + err[:160])
        else:
            self.view.toast('Video gespeichert: ' + path)
        return path, err

    def _record_frame(self):
        if not self.recorder:
            return
        canvas = QtGui.QImage(1024, 768, QtGui.QImage.Format.Format_RGB888)
        canvas.fill(QtGui.QColor(0, 0, 0))
        p = QtGui.QPainter(canvas)
        try:
            if self.scr is not None:
                if self.scr.gfx and self.gfx_image is not None and not self.gfx_image.isNull():
                    if self.gfx_image.width() == 1024 and self.gfx_image.height() == 768:
                        p.drawImage(0, 0, self.gfx_image)
                    else:
                        p.drawImage(QtCore.QRect(0, 0, 1024, 768), self.gfx_image)
                else:
                    img = self.scr.qimage()
                    p.drawImage(max(0, (1024 - img.width()) // 2),
                                max(0, (768 - img.height()) // 2), img)
        finally:
            p.end()
        self.recorder.set_frame(canvas)

    def _save_shot(self):
        if not self.opts['shot'] or not self.scr:
            return
        img = self.scr.qimage().copy()
        img.save(self.opts['shot'], 'PNG')
        print('SHOT', self.opts['shot'])

    @safe
    def closeEvent(self, ev):
        self._closing = True
        if self.recording:
            self.stop_recording()
        self.connection.close()
        super().closeEvent(ev)


class HostDialog(QtWidgets.QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle('TRD - Geraet')
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel('Geraetename oder IP des Ziels:'))
        self.host = QtWidgets.QComboBox()
        self.host.setEditable(True)
        recent = settings.value('recent_hosts', []) or []
        if isinstance(recent, str):
            recent = [recent]
        self.host.addItems([str(h) for h in recent])
        default = str(settings.value('host', 'x61s') or 'x61s')
        self.host.setCurrentText(default)
        lay.addWidget(self.host)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.host.setFocus()

    def value(self):
        return self.host.currentText().strip()


class LoginDialog(QtWidgets.QDialog):
    def __init__(self, settings, host, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.host = host
        self.setWindowTitle('TRD - Anmeldung')
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel('Ziel: %s' % host))
        form = QtWidgets.QFormLayout()
        self.user = QtWidgets.QLineEdit(str(settings.value('user', '') or ''))
        self.auth = QtWidgets.QComboBox()
        self.auth.addItems(['SSH-Schluessel', 'Passwort'])
        self.password = QtWidgets.QLineEdit()
        self.password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.auth.currentIndexChanged.connect(self._auth_changed)
        form.addRow('Benutzer:', self.user)
        form.addRow('Anmeldung:', self.auth)
        form.addRow('Passwort:', self.password)
        lay.addLayout(form)
        saved_meta = str(settings.value('meta_path', DEFAULT_META) or DEFAULT_META)
        if saved_meta.startswith('/tmp/trd-test'):
            saved_meta = DEFAULT_META
        self.meta = QtWidgets.QLineEdit(saved_meta)
        lay.addWidget(QtWidgets.QLabel('TRD-Metadatei auf dem Ziel (normalerweise unveraendert):'))
        lay.addWidget(self.meta)
        self.error = QtWidgets.QLabel()
        self.error.setStyleSheet('color: #d33;')
        self.error.setWordWrap(True)
        lay.addWidget(self.error)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self._auth_changed()
        self.user.setFocus()

    def _auth_changed(self):
        self.password.setEnabled(self.auth.currentIndex() == 1)

    def values(self):
        return (self.user.text().strip(),
                'password' if self.auth.currentIndex() == 1 else 'key',
                self.password.text(),
                self.meta.text().strip())


def main(argv=None):
    ap = argparse.ArgumentParser(description='TRD - Text Remote Desktop fuer Monocell')
    ap.add_argument('--host', default='')
    ap.add_argument('--user', default='')
    ap.add_argument('--auth', choices=('key', 'password'), default='key')
    ap.add_argument('--password', default='')
    ap.add_argument('--meta', default='')
    ap.add_argument('--scale', type=float, default=0.0)
    ap.add_argument('--no-audio', action='store_true')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--shot', default='')
    ap.add_argument('--seconds', type=float, default=3.0)
    ap.add_argument('--send-keys', default='')
    ap.add_argument('--click', default='')
    ap.add_argument('--record', default='')
    ap.add_argument('--snapshot', action='store_true')
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--direct', action='store_true', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.headless or args.shot:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName('trd')
    app.setDesktopFileName('trd')      # task bar: group under the launcher, use its icon
    app.setWindowIcon(icon())
    settings = QtCore.QSettings('trd', 'trd')
    opts = {
        'scale': args.scale, 'headless': args.headless, 'shot': args.shot,
        'seconds': args.seconds, 'send_keys': args.send_keys, 'debug': args.debug,
        'click': args.click, 'record': args.record, 'snapshot': args.snapshot,
    }

    def connect(host, user, auth, password, meta):
        settings.setValue('host', host)
        settings.setValue('user', user)
        recent = settings.value('recent_hosts', []) or []
        if isinstance(recent, str):
            recent = [recent]
        recent = [host] + [h for h in recent if h != host]
        settings.setValue('recent_hosts', recent[:10])
        conn = Connection(host, user, auth, password, meta or None,
                          audio=not args.no_audio, debug=args.debug, direct=args.direct)
        win = MainWindow(conn, opts)
        win.show()
        win.view.setFocus()
        conn.start()
        return win

    if args.host and args.user:
        win = connect(args.host, args.user, args.auth, args.password, args.meta)
        app._trd_window = win
        return app.exec()

    host = args.host
    if not host:
        d = HostDialog(settings)
        if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return 0
        host = d.value()
    else:
        settings.setValue('host', host)
    if not host:
        return 1
    d = LoginDialog(settings, host)
    if args.user:
        d.user.setText(args.user)
    if args.auth:
        d.auth.setCurrentIndex(1 if args.auth == 'password' else 0)
    if args.password:
        d.password.setText(args.password)
    if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return 0
    user, auth, password, meta = d.values()
    if not user:
        return 1
    win = connect(host, user, auth, password, meta)
    app._trd_window = win
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
