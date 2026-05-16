import asyncio
import struct

import davey
import discord.gateway as _gw
import nacl.secret

from utils.logging import logger

IVF_GLOBAL_HEADER_SIZE = 32
IVF_FRAME_HEADER_SIZE = 12
VP8_RTP_PT = 96        # dynamic payload type for VP8
VIDEO_TS_CLOCK = 90_000  # Hz — standard video RTP clock

# ── READY payload parser ───────────────────────────────────────────────────────
# Discord's VOICE READY payload includes a `streams` array that discord.py
# ignores. We patch initial_connection to extract the video SSRC and store it
# as `video_ssrc` on the VoiceConnectionState for use by VideoRTPSender.
#
# Confirmed from live payload:
#   {"streams": [{"type": "video", "ssrc": N+1, "rtx_ssrc": N+2, ...}], "ssrc": N, ...}

_orig_initial_connection = _gw.DiscordVoiceWebSocket.initial_connection


async def _patched_initial_connection(self, data: dict) -> None:
    streams = data.get('streams', [])
    video_stream = next((s for s in streams if s.get('type') == 'video'), None)
    if video_stream:
        self._connection.video_ssrc = video_stream['ssrc']
        logger.info(
            f'[rtp] audio_ssrc={data.get("ssrc")} '
            f'video_ssrc={video_stream["ssrc"]} '
            f'rtx_ssrc={video_stream.get("rtx_ssrc")}'
        )
    else:
        self._connection.video_ssrc = None
        logger.warning('[rtp] VOICE READY contained no video stream entry')
    return await _orig_initial_connection(self, data)


_gw.DiscordVoiceWebSocket.initial_connection = _patched_initial_connection
# ──────────────────────────────────────────────────────────────────────────────


class VideoRTPSender:
    """
    Reads VP8 IVF frames from an FFmpeg subprocess, builds RTP packets,
    applies the same two-layer encryption as discord.py audio
    (davey DAVE E2EE then nacl aead transport), and sends via the
    VoiceClient's UDP socket in parallel with the audio player.

    Lifecycle:
        sender = VideoRTPSender(vc)
        sender.start(video_url, fps=30.0)   # called after vc.play()
        ...
        sender.stop()                        # called after next.wait()
    """

    __slots__ = ('_vc', '_task', '_seq', '_ts', '_nonce')

    def __init__(self, vc) -> None:
        self._vc = vc
        self._task: asyncio.Task | None = None
        self._seq: int = 0
        self._ts: int = 0
        # Independent nonce counter — audio uses vc._incr_nonce; we track our
        # own so the two streams don't race on a shared counter.
        self._nonce: int = 0

    def start(self, video_url: str, fps: float = 30.0) -> None:
        self.stop()
        self._task = asyncio.ensure_future(self._send_loop(video_url, fps))

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _send_loop(self, video_url: str, fps: float) -> None:
        ts_increment = int(VIDEO_TS_CLOCK / fps)
        frame_duration = 1.0 / fps

        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-loglevel', 'quiet',
            '-i', video_url,
            '-vcodec', 'libvpx',
            '-b:v', '1M',
            '-deadline', 'realtime',
            '-cpu-used', '8',
            '-f', 'ivf',
            'pipe:1',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await proc.stdout.readexactly(IVF_GLOBAL_HEADER_SIZE)

            while True:
                frame_header = await proc.stdout.readexactly(IVF_FRAME_HEADER_SIZE)
                frame_size = struct.unpack_from('<I', frame_header, 0)[0]
                vp8_frame = await proc.stdout.readexactly(frame_size)

                packet = self._build_packet(vp8_frame)
                if packet:
                    try:
                        conn = self._vc._connection
                        conn.socket.sendto(
                            packet,
                            (conn.endpoint_ip, conn.voice_port),
                        )
                    except OSError as exc:
                        logger.debug(f'VideoRTPSender: dropped packet — {exc}')

                self._seq = (self._seq + 1) & 0xFFFF
                self._ts = (self._ts + ts_increment) & 0xFFFFFFFF
                await asyncio.sleep(frame_duration)

        except asyncio.IncompleteReadError:
            logger.debug('VideoRTPSender: FFmpeg stdout closed — stream ended')
        except asyncio.CancelledError:
            pass
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _build_packet(self, vp8_frame: bytes) -> bytes | None:
        conn = self._vc._connection
        if not conn.can_encrypt:
            return None

        # Layer 1 — DAVE E2EE (only when a DaveSession is active)
        if conn.dave_session:
            payload = conn.dave_session.encrypt(
                davey.MediaType.video, davey.Codec.vp8, vp8_frame
            )
        else:
            payload = vp8_frame

        # Layer 2 — RTP header (VP8: PT=96, 90kHz clock, video SSRC from READY)
        video_ssrc: int = getattr(conn, 'video_ssrc', None) or (conn.ssrc + 1)
        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x60  # M=0, PT=96
        struct.pack_into('>H', header, 2, self._seq)
        struct.pack_into('>I', header, 4, self._ts)
        struct.pack_into('>I', header, 8, video_ssrc)

        # Layer 3 — transport encryption (replicates _encrypt_aead_xchacha20_poly1305_rtpsize)
        box = nacl.secret.Aead(bytes(conn.secret_key))
        nonce = bytearray(24)
        struct.pack_into('>I', nonce, 0, self._nonce)
        self._nonce = (self._nonce + 1) & 0xFFFFFFFF
        ciphertext = box.encrypt(bytes(payload), bytes(header), bytes(nonce)).ciphertext
        return bytes(header) + ciphertext + bytes(nonce[:4])
