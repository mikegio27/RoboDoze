import asyncio
import struct

import davey
import discord.gateway as _gw
import nacl.secret

from utils.logging import logger

IVF_GLOBAL_HEADER_SIZE = 32
IVF_FRAME_HEADER_SIZE = 12
VP8_RTP_PT = 96            # dynamic payload type for VP8
VIDEO_TS_CLOCK = 90_000    # Hz — standard video RTP clock
MAX_RTP_PAYLOAD = 1100     # safe UDP MTU minus RTP header/auth tag/IPsec overhead

# RTP header byte 1 values (M = marker bit, PT = payload type)
RTP_M0_PT96 = 0x60   # M=0, PT=96 (mid-frame fragment)
RTP_M1_PT96 = 0xE0   # M=1, PT=96 (last fragment of a frame)

# ── READY payload parser ───────────────────────────────────────────────────────
# Discord's VOICE READY payload includes a `streams` array that discord.py
# ignores. We patch initial_connection to extract the video SSRC and stash it
# on the VoiceConnectionState for VideoRTPSender to read later.

_orig_initial_connection = _gw.DiscordVoiceWebSocket.initial_connection


async def _patched_initial_connection(self, data: dict) -> None:
    streams = data.get('streams', [])
    video_stream = next((s for s in streams if s.get('type') == 'video'), None)
    if video_stream:
        self._connection.video_ssrc = video_stream['ssrc']
        logger.info(
            f'[rtp] audio_ssrc={data.get("ssrc")} '
            f'video_ssrc={video_stream["ssrc"]} '
            f'rtx_ssrc={video_stream.get("rtx_ssrc")} '
            f'active={video_stream.get("active")}'
        )
    else:
        self._connection.video_ssrc = None
        logger.warning('[rtp] VOICE READY contained no video stream entry')
    return await _orig_initial_connection(self, data)


_gw.DiscordVoiceWebSocket.initial_connection = _patched_initial_connection


class VideoRTPSender:
    """
    Read VP8 IVF frames from FFmpeg, fragment them per RFC 7741, encrypt each
    fragment (DAVE optional + nacl transport), and send via the VoiceClient's
    UDP socket alongside the audio stream.

    Lifecycle:
        sender = VideoRTPSender(vc)
        sender.start(video_url, fps=30.0)   # called after vc.play()
        ...
        sender.stop()                        # called after next.wait()
    """

    __slots__ = ('_vc', '_task', '_seq', '_ts', '_nonce', '_frame_count', '_packet_count')

    def __init__(self, vc) -> None:
        self._vc = vc
        self._task: asyncio.Task | None = None
        self._seq: int = 0
        self._ts: int = 0
        self._nonce: int = 0    # independent from audio's nonce counter
        self._frame_count: int = 0
        self._packet_count: int = 0

    def start(self, video_url: str, fps: float = 30.0) -> None:
        self.stop()
        self._frame_count = 0
        self._packet_count = 0
        self._task = asyncio.ensure_future(self._send_loop(video_url, fps))

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _send_loop(self, video_url: str, fps: float) -> None:
        conn = self._vc._connection
        video_ssrc: int = getattr(conn, 'video_ssrc', None) or (conn.ssrc + 1)
        logger.info(
            f'[rtp] sender starting: url={video_url[:80]}... fps={fps} '
            f'video_ssrc={video_ssrc} mode={getattr(conn, "mode", "?")} '
            f'dave={"yes" if conn.dave_session else "no"}'
        )

        ts_increment = int(VIDEO_TS_CLOCK / fps)
        frame_duration = 1.0 / fps

        try:
            proc = await asyncio.create_subprocess_exec(
                'ffmpeg', '-loglevel', 'error',
                '-re',                       # read input at native frame rate
                '-i', video_url,
                '-vcodec', 'libvpx',
                '-b:v', '1M',
                '-deadline', 'realtime',
                '-cpu-used', '8',
                '-g', '60',                  # keyframe every 2s at 30fps
                '-error-resilient', '1',
                '-f', 'ivf',
                'pipe:1',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f'[rtp] failed to spawn FFmpeg: {exc!r}')
            return

        assert proc.stdout is not None
        logger.info(f'[rtp] FFmpeg spawned pid={proc.pid}')

        try:
            await proc.stdout.readexactly(IVF_GLOBAL_HEADER_SIZE)
            logger.info('[rtp] IVF global header consumed, awaiting frames')

            while True:
                frame_header = await proc.stdout.readexactly(IVF_FRAME_HEADER_SIZE)
                frame_size = struct.unpack_from('<I', frame_header, 0)[0]
                vp8_frame = await proc.stdout.readexactly(frame_size)
                self._frame_count += 1

                if self._frame_count == 1:
                    logger.info(f'[rtp] first VP8 frame received ({frame_size} bytes)')

                pkts_for_frame = self._send_frame(vp8_frame, video_ssrc)
                self._packet_count += pkts_for_frame

                if self._frame_count % 150 == 0:
                    logger.info(
                        f'[rtp] sent {self._frame_count} frames / '
                        f'{self._packet_count} packets'
                    )

                self._ts = (self._ts + ts_increment) & 0xFFFFFFFF
                await asyncio.sleep(frame_duration)

        except asyncio.IncompleteReadError:
            logger.info(f'[rtp] FFmpeg stdout closed after {self._frame_count} frames')
        except asyncio.CancelledError:
            logger.info(f'[rtp] sender cancelled after {self._frame_count} frames')
            raise
        except Exception as exc:
            logger.exception(f'[rtp] sender crashed: {exc!r}')
        finally:
            if proc.stderr is not None:
                try:
                    stderr_tail = await asyncio.wait_for(proc.stderr.read(2048), timeout=0.5)
                    if stderr_tail:
                        logger.warning(f'[rtp] FFmpeg stderr tail: {stderr_tail.decode(errors="replace")[-1000:]}')
                except (asyncio.TimeoutError, Exception):
                    pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _send_frame(self, vp8_frame: bytes, video_ssrc: int) -> int:
        """Fragment a VP8 frame across one or more RTP packets per RFC 7741.

        Returns the number of UDP packets actually sent for this frame.
        """
        conn = self._vc._connection
        if not conn.can_encrypt:
            return 0

        # Slice the frame into MTU-sized chunks
        fragments: list[bytes] = [
            vp8_frame[i:i + MAX_RTP_PAYLOAD]
            for i in range(0, len(vp8_frame), MAX_RTP_PAYLOAD)
        ] or [b'']

        sent = 0
        for idx, chunk in enumerate(fragments):
            is_first = (idx == 0)
            is_last = (idx == len(fragments) - 1)

            # RFC 7741 §4.2 VP8 payload descriptor (1 byte, no extensions):
            #   X=0 R=0 N=0 S=<1 on first> R=0 PID=000
            descriptor = bytes([0x10 if is_first else 0x00])
            rtp_payload = descriptor + chunk

            # Layer 1 — DAVE end-to-end (if active)
            if conn.dave_session:
                rtp_payload = conn.dave_session.encrypt(
                    davey.MediaType.video, davey.Codec.vp8, rtp_payload
                )

            # Layer 2 — RTP header (marker bit set only on last fragment)
            header = bytearray(12)
            header[0] = 0x80
            header[1] = RTP_M1_PT96 if is_last else RTP_M0_PT96
            struct.pack_into('>H', header, 2, self._seq)
            struct.pack_into('>I', header, 4, self._ts)
            struct.pack_into('>I', header, 8, video_ssrc)
            self._seq = (self._seq + 1) & 0xFFFF

            # Layer 3 — transport encryption (aead_xchacha20_poly1305_rtpsize)
            box = nacl.secret.Aead(bytes(conn.secret_key))
            nonce = bytearray(24)
            struct.pack_into('>I', nonce, 0, self._nonce)
            self._nonce = (self._nonce + 1) & 0xFFFFFFFF
            ciphertext = box.encrypt(bytes(rtp_payload), bytes(header), bytes(nonce)).ciphertext

            packet = bytes(header) + ciphertext + bytes(nonce[:4])

            try:
                conn.socket.sendto(packet, (conn.endpoint_ip, conn.voice_port))
                sent += 1
            except OSError as exc:
                logger.debug(f'[rtp] dropped packet seq={self._seq}: {exc}')

        return sent
