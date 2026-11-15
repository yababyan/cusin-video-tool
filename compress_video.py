from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = None
    ImageOps = None
    ImageTk = None


APP_TITLE = "CUSINI-ROYALE TOOL"
HEADER_IMAGE_FILE = "images.jpg"
WINDOW_ICON_FILE = "DotA2MinimapIcons_AgADagwAAsd2IVA.png"
EXE_ICON_FILE = "cusini_royal_video_tool.ico"
DOTA_LOGO_FILE = "dota2-logo.png"
RETRO_WATERMARK_FILE = "retro-watermark.png"
MODE_ICON_FILES = {
    "discord": "mode-compress.png",
    "retro": "mode-retro.png",
    "gif": "mode-gif.png",
    "custom": "mode-custom.png",
}
DEFAULT_TRIM_DURATION = 60.0
DEFAULT_SIZE_LIMIT_MB = 15
SIZE_LIMIT_OPTIONS = ("10 МБ", "15 МБ", "25 МБ", "50 МБ", "500 МБ")
MAX_TARGET_VIDEO_KBPS = 8000
RETRO_WIDTH = 640
RETRO_HEIGHT = 480
RETRO_FPS = 12


def aspect_crop_filter(ratio_w: int, ratio_h: int) -> str:
    """Центральный кроп под заданное соотношение сторон."""
    return f"crop=w='min(iw,ih*{ratio_w}/{ratio_h})':h='min(ih,iw*{ratio_h}/{ratio_w})'"


RETRO_BASE_FILTER = (
    f"{aspect_crop_filter(4, 3)},"
    f"scale={RETRO_WIDTH}:{RETRO_HEIGHT}:flags=neighbor,fps={RETRO_FPS}"
)


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".wmv",
    ".m4v",
    ".webm",
    ".flv",
    ".mpeg",
    ".mpg",
    ".gif",
}

PRESET_OPTIONS = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]

@dataclass(frozen=True)
class CompressionProfile:
    title: str
    description: str
    crf: int
    preset: str
    width: int | None
    audio_bitrate: str
    no_audio: bool = False
    retro_square: bool = False


@dataclass(frozen=True)
class OperationResult:
    action_name: str
    input_path: Path
    output_path: Path
    details: tuple[str, ...]


class OperationError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


COMPRESSION_PROFILES = [
    CompressionProfile(
        title="Мягкое сжатие",
        description="Примерно минус 20-35% от веса. Качество меняется минимально.",
        crf=28,
        preset="medium",
        width=None,
        audio_bitrate="96k",
    ),
    CompressionProfile(
        title="Стандартное сжатие",
        description="Примерно минус 35-55% от веса. Оптимальный вариант по умолчанию.",
        crf=32,
        preset="slow",
        width=None,
        audio_bitrate="80k",
    ),
    CompressionProfile(
        title="Сильное сжатие",
        description="Примерно минус 55-75% от веса. Уменьшает ширину до 1280px.",
        crf=35,
        preset="slow",
        width=1280,
        audio_bitrate="64k",
    ),
    CompressionProfile(
        title="Квадрат из 2000-х",
        description="Кадр 4:3 640x480, 12 FPS, ретро-качество и watermark в стиле Bandicam.",
        crf=36,
        preset="veryfast",
        width=None,
        audio_bitrate="32k",
        retro_square=True,
    ),
]

COMPRESSION_PROFILE_BY_TITLE = {profile.title: profile for profile in COMPRESSION_PROFILES}


def resource_path(filename: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_dir / filename


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than 0.")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("Value must be 0 or greater.")
    return parsed


def crf_value(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 51:
        raise argparse.ArgumentTypeError("CRF must be between 0 and 51.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Утилита для сжатия и обрезки видео через ffmpeg."
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("gui", help="Запустить графический интерфейс.")
    subparsers.add_parser("menu", help="Запустить интерактивное консольное меню.")

    compress_parser = subparsers.add_parser("compress", help="Сжать видео.")
    add_compress_arguments(compress_parser)

    trim_parser = subparsers.add_parser("trim", help="Обрезать видео по секундам.")
    add_trim_arguments(trim_parser)

    export_parser = subparsers.add_parser(
        "export",
        help="Фрагмент + режим за один проход: discord, retro, gif или custom.",
    )
    add_export_arguments(export_parser)

    return parser


def add_compress_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", type=Path, help="Путь к исходному видеофайлу.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Путь к результату. По умолчанию: <input>_compressed.mp4",
    )
    parser.add_argument(
        "--crf",
        type=crf_value,
        default=28,
        help="Качество H.264. Меньше число -> выше качество. По умолчанию: 28",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        choices=PRESET_OPTIONS,
        help="Скорость кодирования и степень сжатия. По умолчанию: medium",
    )
    parser.add_argument(
        "--width",
        type=positive_int,
        help="Новая ширина видео с сохранением пропорций.",
    )
    parser.add_argument(
        "--audio-bitrate",
        default="96k",
        help="Битрейт AAC-аудио. По умолчанию: 96k",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Удалить аудиодорожку.",
    )


def add_trim_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", type=Path, help="Путь к исходному видеофайлу.")
    parser.add_argument("start", type=non_negative_float, help="С какой секунды начать.")
    parser.add_argument("end", type=non_negative_float, help="На какой секунде закончить.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Путь к результату. По умолчанию: <input>_trimmed.mp4",
    )


def add_export_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", type=Path, help="Путь к видео или GIF.")
    parser.add_argument(
        "--mode",
        choices=("discord", "retro", "gif", "custom"),
        default="discord",
        help="discord — влезть в лимит, retro — шакал 4:3 из 2000-х, gif, custom. По умолчанию: discord",
    )
    parser.add_argument("--start", type=non_negative_float, default=0.0, help="Начало, сек.")
    parser.add_argument("--end", type=non_negative_float, help="Конец, сек. По умолчанию — до конца.")
    parser.add_argument(
        "--limit",
        type=float,
        default=DEFAULT_SIZE_LIMIT_MB,
        help=f"Лимит размера в МБ для discord/retro. По умолчанию: {DEFAULT_SIZE_LIMIT_MB}",
    )
    parser.add_argument("--gif-width", type=positive_int, default=480, help="Ширина GIF.")
    parser.add_argument("--gif-fps", type=positive_int, default=15, help="FPS GIF.")
    parser.add_argument(
        "--gif-aspect", choices=("1:1", "4:3", "16:9"), help="Кроп GIF под соотношение сторон."
    )
    parser.add_argument("-o", "--output", type=Path, help="Путь к результату.")


def expected_output_suffix(input_path: Path) -> str:
    return ".gif" if input_path.suffix.lower() == ".gif" else ".mp4"


def resolve_output_path(
    input_path: Path,
    output_path: Path | None,
    suffix: str,
    *,
    output_suffix: str | None = None,
) -> Path:
    default_suffix = output_suffix or expected_output_suffix(input_path)
    if output_path:
        normalized = normalize_output_path(output_path, default_suffix=default_suffix)
        if normalized.suffix.lower() != default_suffix:
            normalized = normalized.with_suffix(default_suffix)
        return normalized
    return input_path.with_name(f"{input_path.stem}_{suffix}{default_suffix}")


def normalize_output_path(output_path: Path, *, default_suffix: str = ".mp4") -> Path:
    normalized = output_path.expanduser()
    if not normalized.is_absolute():
        normalized = Path.cwd() / normalized
    if not normalized.suffix:
        normalized = normalized.with_suffix(default_suffix)
    return normalized.resolve()


def format_size(size_in_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_in_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_in_bytes} B"


def format_seconds(value: float) -> str:
    formatted = f"{value:.3f}".rstrip("0").rstrip(".")
    return formatted or "0"


def get_ffmpeg_executable() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError as exc:
        raise RuntimeError(
            "ffmpeg не найден. Установите зависимости: python -m pip install -r requirements.txt"
        ) from exc

    return get_ffmpeg_exe()


def build_compress_command(
    args: argparse.Namespace, input_path: Path, output_path: Path
) -> list[str]:
    command = [
        get_ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
        "-i",
        str(input_path),
    ]

    is_retro_square = getattr(args, "retro_square", False)
    input_is_gif = input_path.suffix.lower() == ".gif"
    is_gif = input_is_gif and output_path.suffix.lower() == ".gif"
    output_width = getattr(args, "width", None)
    output_height = getattr(args, "height", None)
    keep_aspect = getattr(args, "keep_aspect", True)

    scale_filter = ""
    if output_width or output_height:
        if output_width and output_height:
            scale_filter = f"scale={output_width}:{output_height}"
            if keep_aspect:
                scale_filter += ":force_original_aspect_ratio=decrease"
        elif output_width:
            scale_filter = f"scale={output_width}:-2"
        else:
            scale_filter = f"scale=-2:{output_height}"

    if is_gif:
        if is_retro_square:
            command.extend(["-i", str(resource_path(RETRO_WATERMARK_FILE))])
            source_filters = (
                f"{RETRO_BASE_FILTER}[base];"
                "[base][1:v]overlay=(W-w)/2:18:format=auto[prepared]"
            )
            max_colors = 96
        else:
            gif_fps = getattr(
                args, "gif_fps", 20 if args.crf <= 28 else 15 if args.crf <= 32 else 12
            )
            filters = []
            if scale_filter:
                filters.append(f"{scale_filter}:flags=lanczos")
            filters.append(f"fps={gif_fps}")
            source_filters = f"{','.join(filters)}[prepared]"
            max_colors = getattr(
                args,
                "gif_colors",
                256 if args.crf <= 28 else 160 if args.crf <= 32 else 112,
            )

        gif_filter = (
            f"[0:v]{source_filters};"
            "[prepared]split[palette_source][gif_source];"
            f"[palette_source]palettegen=max_colors={max_colors}[palette];"
            "[gif_source][palette]paletteuse=dither=bayer:bayer_scale=4[vout]"
        )
        command.extend(
            [
                "-filter_complex",
                gif_filter,
                "-map",
                "[vout]",
                "-an",
                "-loop",
                "0",
                str(output_path),
            ]
        )
        return command

    if is_retro_square:
        command.extend(
            [
                "-i",
                str(resource_path(RETRO_WATERMARK_FILE)),
                "-filter_complex",
                (
                    f"[0:v]{RETRO_BASE_FILTER}[base];"
                    "[base][1:v]overlay=(W-w)/2:18:format=auto[vout]"
                ),
                "-map",
                "[vout]",
                "-map",
                "0:a?",
            ]
        )

    command.extend(
        [
        "-c:v",
        "libx264",
        "-preset",
        args.preset,
        "-crf",
        str(args.crf),
        ]
    )

    if is_retro_square or input_is_gif:
        command.extend(["-pix_fmt", "yuv420p"])
    elif scale_filter:
        command.extend(["-vf", scale_filter])

    if args.no_audio:
        command.append("-an")
    else:
        command.extend(["-c:a", "aac", "-b:a", args.audio_bitrate])
        if is_retro_square:
            command.extend(["-af", "volume=1.2"])

    command.extend(["-movflags", "+faststart", str(output_path)])
    return command


def build_trim_command(input_path: Path, output_path: Path, start: float, end: float) -> list[str]:
    if input_path.suffix.lower() == ".gif":
        return [
            get_ffmpeg_executable(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-stats",
            "-ss",
            format_seconds(start),
            "-to",
            format_seconds(end),
            "-i",
            str(input_path),
            "-filter_complex",
            (
                "[0:v]split[palette_source][gif_source];"
                "[palette_source]palettegen=max_colors=256[palette];"
                "[gif_source][palette]paletteuse=dither=bayer:bayer_scale=4[vout]"
            ),
            "-map",
            "[vout]",
            "-an",
            "-loop",
            "0",
            str(output_path),
        ]

    return [
        get_ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
        "-i",
        str(input_path),
        "-ss",
        format_seconds(start),
        "-to",
        format_seconds(end),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def ensure_input_exists(input_path: Path) -> Path:
    normalized = input_path.expanduser().resolve()
    if not normalized.exists() or not normalized.is_file():
        raise FileNotFoundError(f"Файл не найден: {normalized}")
    return normalized


def validate_output_path(input_path: Path, output_path: Path) -> None:
    if input_path == output_path:
        raise ValueError("Исходный и выходной файлы должны отличаться.")


def run_ffmpeg(command: list[str], action_name: str, *, quiet: bool = False) -> None:
    run_kwargs: dict[str, object] = {"check": True}

    if quiet:
        run_kwargs.update(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        subprocess.run(command, **run_kwargs)
    except RuntimeError as exc:
        raise OperationError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        error_text = ""
        if quiet:
            stderr = (exc.stderr or "").strip()
            if stderr:
                error_text = f"\n\n{stderr[-2000:]}"
        raise OperationError(
            f"{action_name} завершилось с ошибкой, код: {exc.returncode}.{error_text}",
            exit_code=exc.returncode,
        ) from exc


def compression_summary_lines(input_path: Path, output_path: Path) -> list[str]:
    source_size = input_path.stat().st_size
    output_size = output_path.stat().st_size
    saved_bytes = source_size - output_size
    saved_percent = (saved_bytes / source_size * 100) if source_size else 0

    lines = [
        f"Исходный размер: {format_size(source_size)}",
        f"Новый размер: {format_size(output_size)}",
    ]

    if saved_bytes >= 0:
        lines.append(f"Экономия: {format_size(saved_bytes)} ({saved_percent:.1f}%)")
    else:
        lines.append(
            f"Файл стал больше на: {format_size(abs(saved_bytes))} ({abs(saved_percent):.1f}%)"
        )
    return lines


def process_compress(args: argparse.Namespace, *, quiet: bool = False) -> OperationResult:
    try:
        input_path = ensure_input_exists(args.input)
        output_path = resolve_output_path(input_path, args.output, "compressed")
        validate_output_path(input_path, output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except (FileNotFoundError, ValueError) as exc:
        raise OperationError(str(exc)) from exc

    details = [
        f"Сжатие файла: {input_path.name}",
        f"Результат: {output_path.name}",
    ]

    command = build_compress_command(args, input_path, output_path)
    run_ffmpeg(command, "Сжатие", quiet=quiet)
    details.extend(compression_summary_lines(input_path, output_path))

    return OperationResult(
        action_name="Сжатие",
        input_path=input_path,
        output_path=output_path,
        details=tuple(details),
    )


def process_trim(args: argparse.Namespace, *, quiet: bool = False) -> OperationResult:
    if args.end <= args.start:
        raise OperationError("Конечная секунда должна быть больше начальной.")

    try:
        input_path = ensure_input_exists(args.input)
        output_path = resolve_output_path(input_path, args.output, "trimmed")
        validate_output_path(input_path, output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except (FileNotFoundError, ValueError) as exc:
        raise OperationError(str(exc)) from exc

    details = [
        f"Обрезка файла: {input_path.name}",
        f"От: {format_seconds(args.start)} сек",
        f"До: {format_seconds(args.end)} сек",
        f"Результат: {output_path.name}",
    ]

    command = build_trim_command(input_path, output_path, args.start, args.end)
    run_ffmpeg(command, "Обрезка", quiet=quiet)
    details.append(f"Готово: {output_path}")

    return OperationResult(
        action_name="Обрезка",
        input_path=input_path,
        output_path=output_path,
        details=tuple(details),
    )


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool


def probe_media(input_path: Path) -> MediaInfo:
    """Читает длительность, размер кадра, FPS и наличие звука через `ffmpeg -i`.

    ffprobe в сборке нет (imageio-ffmpeg ставит только ffmpeg), поэтому
    разбираем stderr самого ffmpeg.
    """
    try:
        command = [get_ffmpeg_executable(), "-hide_banner", "-i", str(input_path)]
    except RuntimeError:
        return MediaInfo(None, None, None, None, False)

    run_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        output = subprocess.run(command, **run_kwargs).stderr or ""
    except OSError:
        return MediaInfo(None, None, None, None, False)

    duration = None
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if duration <= 0:
            duration = None

    width = height = None
    fps = None
    video_line = next(
        (line for line in output.splitlines() if "Stream #" in line and "Video:" in line),
        "",
    )
    size_match = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video_line)
    if size_match:
        width, height = int(size_match.group(1)), int(size_match.group(2))
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", video_line) or re.search(
        r"(\d+(?:\.\d+)?)\s*tbr", video_line
    )
    if fps_match:
        fps = float(fps_match.group(1))

    has_audio = any(
        "Stream #" in line and "Audio:" in line for line in output.splitlines()
    )
    return MediaInfo(duration, width, height, fps, has_audio)


def parse_size_limit(text: str) -> float:
    return float(text.split()[0].replace(",", "."))


def size_limit_bytes(limit_mb: float) -> int:
    # Считаем в десятичных мегабайтах: так результат точно пролезет
    # в лимит Discord, как бы он ни считал мегабайты.
    return int(limit_mb * 1000 * 1000)


@dataclass(frozen=True)
class BitratePlan:
    video_kbps: int
    audio_kbps: int
    max_short_side: int | None
    fps_cap: int | None


def plan_bitrate(
    duration: float,
    limit_mb: float,
    *,
    has_audio: bool,
    info: MediaInfo | None = None,
    fixed_frame: bool = False,
) -> BitratePlan:
    """Подбирает битрейт, разрешение и FPS, чтобы фрагмент влез в лимит."""
    budget_kbps = size_limit_bytes(limit_mb) * 8 / 1000 / duration * 0.94
    if not has_audio:
        audio_kbps = 0
    elif budget_kbps > 900:
        audio_kbps = 96
    elif budget_kbps > 300:
        audio_kbps = 64
    else:
        audio_kbps = 32

    video_kbps = int(min(budget_kbps - audio_kbps, MAX_TARGET_VIDEO_KBPS))
    if video_kbps < 60:
        max_seconds = size_limit_bytes(limit_mb) * 8 / 1000 * 0.94 / (60 + audio_kbps)
        raise OperationError(
            f"Фрагмент {format_duration(duration)} слишком длинный для "
            f"{format_limit(limit_mb)}. Сократите его примерно до "
            f"{format_duration(max_seconds)} или выберите лимит побольше."
        )

    max_short_side = None
    fps_cap = None
    if not fixed_frame:
        if video_kbps >= 4000:
            max_short_side = 1080
        elif video_kbps >= 1800:
            max_short_side = 720
        elif video_kbps >= 700:
            max_short_side = 480
        else:
            max_short_side = 360
        source_fps = info.fps if info else None
        if video_kbps < 3000 and (source_fps is None or source_fps > 31):
            fps_cap = 30
        if info and info.width and info.height:
            short_side = min(info.width, info.height)
            if short_side <= max_short_side:
                max_short_side = None
            # Короткому ролику не нужен весь бюджет: ~0.12 бита на пиксель
            # уже даёт чистую картинку, лишние мегабайты только мешают.
            scale = (max_short_side / short_side) if max_short_side else 1.0
            out_fps = min(source_fps or 30, fps_cap or 240)
            pixels = info.width * info.height * scale * scale
            useful_kbps = int(pixels * out_fps * 0.12 / 1000)
            video_kbps = min(video_kbps, max(600, useful_kbps))
    return BitratePlan(video_kbps, audio_kbps, max_short_side, fps_cap)


def format_limit(limit_mb: float) -> str:
    return f"{format_seconds(limit_mb)} МБ"


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, rest = divmod(seconds, 60)
    if minutes >= 1:
        return f"{int(minutes)}:{rest:04.1f}"
    return f"{rest:.1f} сек"


def describe_plan(plan: BitratePlan, info: MediaInfo | None) -> str:
    parts = [f"видео ~{plan.video_kbps} кбит/с"]
    if plan.audio_kbps:
        parts.append(f"звук {plan.audio_kbps} кбит/с")
    if plan.max_short_side:
        parts.append(f"{plan.max_short_side}p")
    elif info and info.width and info.height:
        parts.append(f"{info.width}×{info.height}")
    if plan.fps_cap:
        parts.append(f"{plan.fps_cap} FPS")
    return " · ".join(parts)


EXPORT_MODES = ("discord", "retro", "gif", "custom")
GIF_ASPECTS = {"Исходный": None, "1:1": (1, 1), "4:3": (4, 3), "16:9": (16, 9)}


@dataclass
class ExportJob:
    input_path: Path
    mode: str
    output_path: Path | None = None
    start: float = 0.0
    end: float | None = None
    size_limit_mb: float | None = DEFAULT_SIZE_LIMIT_MB
    # GIF
    gif_width: int | None = 480
    gif_fps: int = 15
    gif_colors: int = 128
    gif_aspect: str = "Исходный"
    gif_target_mb: float | None = None
    # Своё сжатие
    profile_title: str = "Стандартное сжатие"
    width: int | None = None
    height: int | None = None
    keep_aspect: bool = True


def export_output_suffix(mode: str) -> str:
    return ".gif" if mode == "gif" else ".mp4"


def export_output_tag(mode: str, input_path: Path) -> str:
    if mode == "gif":
        return "optimized" if input_path.suffix.lower() == ".gif" else "gif"
    return {"discord": "discord", "retro": "2000s", "custom": "compressed"}[mode]


def default_export_output(job: ExportJob) -> Path:
    return resolve_output_path(
        job.input_path,
        None,
        export_output_tag(job.mode, job.input_path),
        output_suffix=export_output_suffix(job.mode),
    )


def fragment_input_args(job: ExportJob, info: MediaInfo) -> list[str]:
    args: list[str] = []
    if job.start > 0:
        args.extend(["-ss", format_seconds(job.start)])
    if job.end is not None and (info.duration is None or job.end < info.duration - 0.05):
        args.extend(["-t", format_seconds(job.end - job.start)])
    return args


def fragment_duration(job: ExportJob, info: MediaInfo) -> float | None:
    end = job.end if job.end is not None else info.duration
    if end is None:
        return None
    if info.duration is not None:
        end = min(end, info.duration)
    return max(0.0, end - job.start)


def short_side_scale(max_short_side: int) -> str:
    return (
        f"scale=w='if(gte(iw,ih),-2,{max_short_side})':"
        f"h='if(gte(iw,ih),{max_short_side},-2)'"
    )


def build_export_video_command(
    job: ExportJob,
    info: MediaInfo,
    output_path: Path,
    *,
    crf: int | None = None,
    plan: BitratePlan | None = None,
    pass_number: int | None = None,
    passlog: Path | None = None,
) -> list[str]:
    retro = job.mode == "retro"
    command = [
        get_ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        *fragment_input_args(job, info),
        "-i",
        str(job.input_path),
    ]

    if retro:
        command.extend(
            [
                "-i",
                str(resource_path(RETRO_WATERMARK_FILE)),
                "-filter_complex",
                (
                    f"[0:v]{RETRO_BASE_FILTER}[base];"
                    "[base][1:v]overlay=(W-w)/2:18:format=auto,format=yuv420p[vout]"
                ),
                "-map",
                "[vout]",
                "-map",
                "0:a?",
            ]
        )
    else:
        filters: list[str] = []
        if job.mode == "custom":
            if not job.width and not job.height:
                job.width = COMPRESSION_PROFILE_BY_TITLE[job.profile_title].width
            if job.width and job.height:
                scale = f"scale={job.width}:{job.height}"
                if job.keep_aspect:
                    scale += ":force_original_aspect_ratio=decrease"
                filters.append(scale)
                filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
            elif job.width:
                filters.append(f"scale='min({job.width},iw)':-2")
            elif job.height:
                filters.append(f"scale=-2:{job.height}")
        if plan is not None:
            if plan.max_short_side:
                filters.append(short_side_scale(plan.max_short_side))
            if plan.fps_cap:
                filters.append(f"fps={plan.fps_cap}")
        # Чётные размеры нужны libx264 (например, для GIF с нечётной шириной).
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        filters.append("format=yuv420p")
        command.extend(["-vf", ",".join(filters), "-map", "0:v:0", "-map", "0:a?"])

    preset = "veryfast" if retro and plan is None else "medium"
    command.extend(["-c:v", "libx264", "-preset", preset])
    if plan is not None:
        command.extend(
            [
                "-b:v",
                f"{plan.video_kbps}k",
                "-maxrate",
                f"{int(plan.video_kbps * 1.5)}k",
                "-bufsize",
                f"{plan.video_kbps * 2}k",
            ]
        )
    else:
        command.extend(["-crf", str(crf if crf is not None else 28)])

    if pass_number is not None and passlog is not None:
        command.extend(["-pass", str(pass_number), "-passlogfile", str(passlog)])

    if pass_number == 1:
        command.extend(["-an", "-f", "null", os.devnull])
        return command

    if not info.has_audio:
        command.append("-an")
    else:
        if plan is not None:
            audio_bitrate = f"{plan.audio_kbps}k"
        elif retro:
            audio_bitrate = "32k"
        else:
            audio_bitrate = COMPRESSION_PROFILE_BY_TITLE[job.profile_title].audio_bitrate
        command.extend(["-c:a", "aac", "-b:a", audio_bitrate])
        if retro:
            command.extend(["-af", "volume=1.2"])

    command.extend(["-movflags", "+faststart", str(output_path)])
    return command


def build_export_gif_command(
    job: ExportJob, info: MediaInfo, output_path: Path, *, fps: int, colors: int
) -> list[str]:
    filters: list[str] = []
    aspect = GIF_ASPECTS.get(job.gif_aspect)
    if aspect:
        filters.append(aspect_crop_filter(*aspect))
    if job.gif_width:
        filters.append(f"scale='min({job.gif_width},iw)':-2:flags=lanczos")
    filters.append(f"fps={fps}")
    gif_filter = (
        f"[0:v]{','.join(filters)},split[palette_source][gif_source];"
        f"[palette_source]palettegen=max_colors={colors}:stats_mode=diff[palette];"
        "[gif_source][palette]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle[vout]"
    )
    return [
        get_ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        *fragment_input_args(job, info),
        "-i",
        str(job.input_path),
        "-filter_complex",
        gif_filter,
        "-map",
        "[vout]",
        "-an",
        "-loop",
        "0",
        str(output_path),
    ]


ProgressCallback = Callable[[float], None] | None


def run_ffmpeg_with_progress(
    command: list[str],
    action_name: str,
    *,
    duration: float | None,
    on_progress: ProgressCallback = None,
    progress_range: tuple[float, float] = (0.0, 1.0),
    cwd: Path | None = None,
) -> None:
    """Запускает ffmpeg с `-progress pipe:1` и сообщает долю выполнения."""
    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd) if cwd else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        raise OperationError(f"Не удалось запустить ffmpeg: {exc}") from exc

    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        assert process.stderr is not None
        stderr_chunks.append(process.stderr.read())

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    low, high = progress_range
    assert process.stdout is not None
    for line in process.stdout:
        if not on_progress or not duration:
            continue
        key, _, value = line.strip().partition("=")
        if key in {"out_time_us", "out_time_ms"} and value.isdigit():
            fraction = min(1.0, int(value) / 1_000_000 / duration)
            on_progress(low + (high - low) * fraction)

    return_code = process.wait()
    stderr_thread.join(timeout=5)
    if return_code != 0:
        stderr = "".join(stderr_chunks).strip()
        error_text = f"\n\n{stderr[-2000:]}" if stderr else ""
        raise OperationError(
            f"{action_name} завершилось с ошибкой, код: {return_code}.{error_text}",
            exit_code=return_code,
        )
    if on_progress:
        on_progress(high)


def encode_to_size(
    job: ExportJob,
    info: MediaInfo,
    output_path: Path,
    duration: float,
    *,
    on_progress: ProgressCallback,
    progress_range: tuple[float, float],
) -> tuple[BitratePlan, int]:
    """Двухпроходное кодирование под лимит; при перелёте повторяет с меньшим битрейтом."""
    assert job.size_limit_mb is not None
    limit_bytes = size_limit_bytes(job.size_limit_mb)
    plan = plan_bitrate(
        duration,
        job.size_limit_mb,
        has_audio=info.has_audio,
        info=info,
        fixed_frame=job.mode == "retro",
    )
    low, high = progress_range
    middle = (low + high) / 2
    attempts = 3
    with tempfile.TemporaryDirectory(prefix="cusini_") as temp_dir:
        # Относительный путь + cwd: x264 под Windows не открывает
        # файл статистики, если в пути есть кириллица.
        passlog = Path("x264pass")
        for attempt in range(attempts):
            for pass_number, sub_range in ((1, (low, middle)), (2, (middle, high))):
                run_ffmpeg_with_progress(
                    build_export_video_command(
                        job,
                        info,
                        output_path,
                        plan=plan,
                        pass_number=pass_number,
                        passlog=passlog,
                    ),
                    "Кодирование",
                    duration=duration,
                    on_progress=on_progress,
                    progress_range=sub_range,
                    cwd=Path(temp_dir),
                )
            actual = output_path.stat().st_size
            if actual <= limit_bytes:
                return plan, attempt + 1
            ratio = limit_bytes / actual * 0.97
            plan = BitratePlan(
                max(50, int(plan.video_kbps * ratio)),
                plan.audio_kbps,
                plan.max_short_side,
                plan.fps_cap,
            )
    return plan, attempts


def process_export(job: ExportJob, *, on_progress: ProgressCallback = None) -> OperationResult:
    if job.mode not in EXPORT_MODES:
        raise OperationError(f"Неизвестный режим: {job.mode}")
    try:
        input_path = ensure_input_exists(job.input_path)
        job.input_path = input_path
        output_path = resolve_output_path(
            input_path,
            job.output_path,
            export_output_tag(job.mode, input_path),
            output_suffix=export_output_suffix(job.mode),
        )
        validate_output_path(input_path, output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except (FileNotFoundError, ValueError) as exc:
        raise OperationError(str(exc)) from exc

    if job.end is not None and job.end <= job.start:
        raise OperationError("Конец фрагмента должен быть позже начала.")

    info = probe_media(input_path)
    duration = fragment_duration(job, info)
    if duration is not None and duration <= 0:
        raise OperationError("Начало фрагмента находится за концом видео.")

    action_name = {
        "discord": "Сжатие для Discord",
        "retro": "Шакал 2000-х",
        "gif": "GIF",
        "custom": "Сжатие",
    }[job.mode]
    details = [f"Файл: {input_path.name}"]
    if fragment_input_args(job, info) and duration is not None:
        details.append(
            f"Фрагмент: {format_seconds(job.start)}–"
            f"{format_seconds(job.start + duration)} сек ({format_duration(duration)})"
        )

    if job.mode == "gif":
        attempts = [(job.gif_fps, job.gif_colors)]
        if job.gif_target_mb:
            attempts.extend(
                (min(job.gif_fps, fps), min(job.gif_colors, colors))
                for fps, colors in ((15, 128), (12, 96), (10, 64), (8, 48), (6, 32))
            )
            attempts = list(dict.fromkeys(attempts))
        target_bytes = size_limit_bytes(job.gif_target_mb) if job.gif_target_mb else None
        for index, (fps, colors) in enumerate(attempts):
            run_ffmpeg_with_progress(
                build_export_gif_command(job, info, output_path, fps=fps, colors=colors),
                action_name,
                duration=duration,
                on_progress=on_progress,
                progress_range=(index / len(attempts), (index + 1) / len(attempts)),
            )
            if target_bytes is None or output_path.stat().st_size <= target_bytes:
                details.append(f"GIF: {fps} FPS, {colors} цветов")
                break
        else:
            details.append(
                "Целевой вес недостижим с безопасными настройками; сохранён минимальный вариант."
            )
    elif job.mode in {"discord", "retro"} and job.size_limit_mb:
        if duration is None:
            raise OperationError(
                "Не удалось определить длительность видео — без неё нельзя подобрать битрейт."
            )
        limit_bytes = size_limit_bytes(job.size_limit_mb)
        fitted = False
        if job.mode == "retro":
            # Сначала шакалим по CRF — обычно этого хватает с запасом.
            run_ffmpeg_with_progress(
                build_export_video_command(job, info, output_path, crf=36),
                action_name,
                duration=duration,
                on_progress=on_progress,
                progress_range=(0.0, 0.5),
            )
            fitted = output_path.stat().st_size <= limit_bytes
            if fitted and on_progress:
                on_progress(1.0)
        if not fitted:
            plan, passes = encode_to_size(
                job,
                info,
                output_path,
                duration,
                on_progress=on_progress,
                progress_range=(0.5 if job.mode == "retro" else 0.0, 1.0),
            )
            frame_info = (
                MediaInfo(None, RETRO_WIDTH, RETRO_HEIGHT, RETRO_FPS, info.has_audio)
                if job.mode == "retro"
                else info
            )
            details.append(f"Битрейт: {describe_plan(plan, frame_info)}")
            if passes > 1:
                details.append(f"Понадобилось попыток: {passes}")
    else:
        crf = (
            36
            if job.mode == "retro"
            else COMPRESSION_PROFILE_BY_TITLE[job.profile_title].crf
        )
        run_ffmpeg_with_progress(
            build_export_video_command(job, info, output_path, crf=crf),
            action_name,
            duration=duration,
            on_progress=on_progress,
        )

    details.append(f"Результат: {output_path.name}")
    details.extend(compression_summary_lines(input_path, output_path))
    if job.size_limit_mb and job.mode in {"discord", "retro"}:
        fits = output_path.stat().st_size <= size_limit_bytes(job.size_limit_mb)
        details.append(
            f"Лимит {format_limit(job.size_limit_mb)}: {'влезает' if fits else 'НЕ влезает'}"
        )

    return OperationResult(
        action_name=action_name,
        input_path=input_path,
        output_path=output_path,
        details=tuple(details),
    )


def print_operation_result(result: OperationResult) -> None:
    for line in result.details:
        print(line)


def run_compress(args: argparse.Namespace) -> int:
    try:
        result = process_compress(args)
    except OperationError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code

    print_operation_result(result)
    return 0


def run_trim(args: argparse.Namespace) -> int:
    try:
        result = process_trim(args)
    except OperationError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code

    print_operation_result(result)
    return 0


def run_export(args: argparse.Namespace) -> int:
    job = ExportJob(
        input_path=args.input,
        mode=args.mode,
        output_path=args.output,
        start=args.start,
        end=args.end,
        size_limit_mb=args.limit,
        gif_width=args.gif_width,
        gif_fps=args.gif_fps,
        gif_aspect=args.gif_aspect or "Исходный",
    )
    try:
        result = process_export(job)
    except OperationError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code

    print_operation_result(result)
    return 0


def clear_screen() -> None:
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


def pause() -> None:
    if sys.stdin.isatty() and sys.stdout.isatty():
        input("\nНажмите Enter, чтобы продолжить...")


def print_header() -> None:
    print("=" * 60)
    print(" Video Tool")
    print(" Сжатие и обрезка видео через FFmpeg")
    print("=" * 60)


def prompt_choice(prompt: str, valid_choices: set[str]) -> str:
    while True:
        choice = input(prompt).strip()
        if choice in valid_choices:
            return choice
        print("Введите корректный номер.")


def prompt_number(
    prompt: str,
    *,
    default: int | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
    allow_zero: bool = True,
) -> int:
    while True:
        raw_value = input(prompt).strip()
        if not raw_value and default is not None:
            return default

        try:
            value = int(raw_value)
        except ValueError:
            print("Введите целое число.")
            continue

        if value == 0 and allow_zero:
            return value
        if min_value is not None and value < min_value:
            print(f"Введите число не меньше {min_value}.")
            continue
        if max_value is not None and value > max_value:
            print(f"Введите число не больше {max_value}.")
            continue
        return value


def prompt_seconds_input(prompt: str) -> float:
    while True:
        raw_value = input(prompt).strip().replace(",", ".")
        try:
            value = float(raw_value)
        except ValueError:
            print("Введите число секунд, например 12 или 12.5.")
            continue

        if value < 0:
            print("Значение не может быть отрицательным.")
            continue
        return value


def prompt_output_path(input_path: Path, suffix: str) -> Path:
    default_path = resolve_output_path(input_path, None, suffix)
    raw_value = input(
        f"Путь для результата [Enter = {default_path.name}]: "
    ).strip()
    if not raw_value:
        return default_path
    default_suffix = expected_output_suffix(input_path)
    return normalize_output_path(Path(raw_value.strip('"')), default_suffix=default_suffix)


def find_video_files(directory: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda path: path.name.lower(),
    )


def prompt_video_file() -> Path | None:
    while True:
        clear_screen()
        print_header()
        print(f"Текущая папка: {Path.cwd()}")
        print()

        files = find_video_files(Path.cwd())
        for index, path in enumerate(files, start=1):
            print(f"{index}. {path.name} ({format_size(path.stat().st_size)})")

        manual_choice = len(files) + 1
        refresh_choice = len(files) + 2

        if not files:
                print("Видео и GIF в текущей папке не найдены.")

        print(f"{manual_choice}. Ввести путь вручную")
        print(f"{refresh_choice}. Обновить список")
        print("0. Назад")

        choice = prompt_choice("\nВаш выбор: ", {str(index) for index in range(refresh_choice + 1)})

        if choice == "0":
            return None
        if choice == str(refresh_choice):
            continue
        if choice == str(manual_choice):
            raw_path = input("Введите путь к видео: ").strip().strip('"')
            if not raw_path:
                print("Путь не введен.")
                pause()
                continue
            try:
                return ensure_input_exists(Path(raw_path))
            except FileNotFoundError as exc:
                print(str(exc))
                pause()
                continue

        selected_file = files[int(choice) - 1]
        return selected_file.resolve()


def prompt_preset() -> str:
    while True:
        print("\nВыберите preset:")
        for index, preset in enumerate(PRESET_OPTIONS, start=1):
            default_mark = " [по умолчанию]" if preset == "medium" else ""
            print(f"{index}. {preset}{default_mark}")

        choice = prompt_choice("Ваш выбор: ", {str(index) for index in range(1, len(PRESET_OPTIONS) + 1)})
        return PRESET_OPTIONS[int(choice) - 1]


def prompt_compression_profile() -> CompressionProfile | argparse.Namespace | None:
    while True:
        clear_screen()
        print_header()
        print("Режим сжатия:\n")
        for index, profile in enumerate(COMPRESSION_PROFILES, start=1):
            print(f"{index}. {profile.title}")
            print(f"   {profile.description}")
        custom_choice = len(COMPRESSION_PROFILES) + 1
        print(f"{custom_choice}. Свои настройки")
        print("0. Назад")

        choice = prompt_choice(
            "\nВаш выбор: ",
            {str(index) for index in range(custom_choice + 1)},
        )

        if choice == "0":
            return None
        if choice != str(custom_choice):
            return COMPRESSION_PROFILES[int(choice) - 1]

        crf = prompt_number(
            "\nCRF (0-51, меньше = лучше качество) [28]: ",
            default=28,
            min_value=0,
            max_value=51,
        )
        preset = prompt_preset()
        width = prompt_number(
            "Ширина видео, 0 = оставить исходную [0]: ",
            default=0,
            min_value=0,
        )

        print("\nАудио:")
        print("1. Оставить и сжать до 96k")
        print("2. Оставить и сжать до 64k")
        print("3. Удалить аудио")
        audio_choice = prompt_choice("Ваш выбор: ", {"1", "2", "3"})

        no_audio = audio_choice == "3"
        audio_bitrate = "96k" if audio_choice == "1" else "64k"

        return argparse.Namespace(
            crf=crf,
            preset=preset,
            width=width or None,
            audio_bitrate=audio_bitrate,
            no_audio=no_audio,
        )


def run_interactive_compress() -> int:
    input_path = prompt_video_file()
    if input_path is None:
        return 0

    profile = prompt_compression_profile()
    if profile is None:
        return 0

    output_path = prompt_output_path(input_path, "compressed")
    args = argparse.Namespace(
        input=input_path,
        output=output_path,
        crf=profile.crf,
        preset=profile.preset,
        width=profile.width,
        audio_bitrate=profile.audio_bitrate,
        no_audio=profile.no_audio,
        retro_square=profile.retro_square,
    )

    clear_screen()
    print_header()
    exit_code = run_compress(args)
    pause()
    return exit_code


def run_interactive_trim() -> int:
    input_path = prompt_video_file()
    if input_path is None:
        return 0

    clear_screen()
    print_header()
    print(f"Файл: {input_path.name}\n")

    start = prompt_seconds_input("С какой секунды начать: ")
    while True:
        end = prompt_seconds_input("На какой секунде закончить: ")
        if end > start:
            break
        print("Конечная секунда должна быть больше начальной.")

    output_path = prompt_output_path(input_path, "trimmed")
    args = argparse.Namespace(input=input_path, output=output_path, start=start, end=end)

    clear_screen()
    print_header()
    exit_code = run_trim(args)
    pause()
    return exit_code


def run_menu() -> int:
    while True:
        clear_screen()
        print_header()
        print("1. Сжать видео")
        print("2. Обрезать видео")
        print("0. Выход")

        choice = prompt_choice("\nВаш выбор: ", {"0", "1", "2"})
        if choice == "0":
            print("Выход.")
            return 0
        if choice == "1":
            run_interactive_compress()
            continue
        run_interactive_trim()


if tk is not None and ttk is not None and filedialog is not None and messagebox is not None:
    class VideoToolApp:
        MODE_SPECS = (
            ("discord", "В DISCORD", "влезет в лимит"),
            ("retro", "ШАКАЛ 2000-Х", "4:3 · 12 FPS · Bandicam"),
            ("gif", "GIF", "из видео или GIF"),
            ("custom", "СВОЁ", "ручные настройки"),
        )
        RUN_LABELS = {
            "discord": "▶  СЖАТЬ ДЛЯ DISCORD",
            "retro": "▶  СДЕЛАТЬ ШАКАЛ 2000-Х",
            "gif": "▶  СДЕЛАТЬ GIF",
            "custom": "▶  СЖАТЬ",
        }

        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title(APP_TITLE)
            self.root.geometry("1180x780")
            self.root.minsize(1060, 740)

            self.selected_input: Path | None = None
            self.media_info: MediaInfo | None = None
            self.custom_output: Path | None = None
            self.last_output_path: Path | None = None
            self.worker_thread: threading.Thread | None = None
            self.is_busy = False
            self.header_photo: object | None = None
            self.dota_logo_photo: object | None = None
            self.window_icon_photo: object | None = None
            self.mode_photos: dict[str, object] = {}
            self.trim_duration = DEFAULT_TRIM_DURATION
            self._syncing_trim_controls = False

            self.input_path_var = tk.StringVar()
            self.input_info_var = tk.StringVar(
                value="Файл не выбран. Подойдут MP4, MOV, MKV, WEBM, AVI, GIF и другие."
            )
            self.status_var = tk.StringVar(value="Выберите файл, фрагмент и что с ним сделать.")
            self.mode_var = tk.StringVar(value="discord")
            self.limit_var = tk.StringVar(value=f"{DEFAULT_SIZE_LIMIT_MB} МБ")
            self.plan_var = tk.StringVar()
            self.output_var = tk.StringVar()
            self.profile_var = tk.StringVar(value=COMPRESSION_PROFILES[1].title)
            self.profile_description_var = tk.StringVar()
            self.resize_mode_var = tk.StringVar(value="Как в пресете")
            self.resize_width_var = tk.StringVar()
            self.resize_height_var = tk.StringVar()
            self.keep_aspect_var = tk.BooleanVar(value=True)
            self.gif_width_var = tk.StringVar(value="480 px")
            self.gif_aspect_var = tk.StringVar(value="Исходный")
            self.gif_fps_var = tk.StringVar(value="15")
            self.gif_colors_var = tk.StringVar(value="128")
            self.gif_target_mb_var = tk.StringVar()
            self.trim_start_var = tk.StringVar(value="0")
            self.trim_end_var = tk.StringVar(value=format_seconds(DEFAULT_TRIM_DURATION))
            self.trim_start_scale_var = tk.DoubleVar(value=0)
            self.trim_end_scale_var = tk.DoubleVar(value=DEFAULT_TRIM_DURATION)
            self.trim_selection_var = tk.StringVar(value="Всё видео")

            self.interactive_widgets: list[object] = []

            self._configure_style()
            self._load_assets()
            self._build_ui()
            self._refresh_profile_state()
            self._select_mode("discord")

        def _configure_style(self) -> None:
            style = ttk.Style()
            style.theme_use("clam")

            self.colors = {
                "bg": "#080B0D",
                "top": "#0C1115",
                "panel": "#10161B",
                "panel_alt": "#0B1014",
                "border": "#283138",
                "text": "#DDD9D0",
                "muted": "#8D9191",
                "red": "#C8442D",
                "red_hover": "#E4583E",
                "green": "#4D702C",
                "green_hover": "#608A37",
                "cyan": "#9FC4D1",
            }
            colors = self.colors
            self.root.configure(background=colors["bg"])

            ui_font = ("Segoe UI", 10)
            heading_font = ("Segoe UI Semibold", 12)
            self.root.option_add("*Font", ui_font)
            style.configure(".", background=colors["bg"], foreground=colors["text"], font=ui_font)
            style.configure("App.TFrame", background=colors["bg"])
            style.configure("Top.TFrame", background=colors["top"])
            style.configure("Panel.TFrame", background=colors["panel"])
            style.configure("Inset.TFrame", background=colors["panel_alt"])
            style.configure(
                "Title.TLabel",
                background=colors["top"],
                foreground="#EEEAE0",
                font=("Segoe UI Semibold", 22),
            )
            style.configure(
                "Brand.TLabel",
                background=colors["red"],
                foreground="#17100D",
                font=("Segoe UI Semibold", 22),
                padding=(14, 8),
            )
            style.configure(
                "Nav.TLabel",
                background=colors["top"],
                foreground=colors["muted"],
                font=("Segoe UI", 9, "bold"),
            )
            style.configure(
                "NavActive.TLabel",
                background=colors["top"],
                foreground="#F1E9DC",
                font=("Segoe UI", 9, "bold"),
            )
            style.configure(
                "SectionTitle.TLabel",
                background=colors["panel"],
                foreground=colors["cyan"],
                font=heading_font,
            )
            style.configure(
                "Body.TLabel",
                background=colors["panel"],
                foreground=colors["text"],
            )
            style.configure("Hint.TLabel", background=colors["panel"], foreground=colors["muted"])
            style.configure(
                "Setting.TLabel",
                background=colors["panel_alt"],
                foreground=colors["text"],
                font=ui_font,
            )
            style.configure(
                "SettingHint.TLabel",
                background=colors["panel_alt"],
                foreground=colors["muted"],
                font=("Segoe UI", 9),
            )
            style.configure(
                "SettingTitle.TLabel",
                background=colors["panel_alt"],
                foreground="#D8D3C8",
                font=("Segoe UI Semibold", 10),
            )
            style.configure(
                "SettingValue.TLabel",
                background=colors["panel_alt"],
                foreground=colors["cyan"],
                font=("Segoe UI Semibold", 10),
            )
            style.configure(
                "Setting.TCheckbutton",
                background=colors["panel_alt"],
                foreground=colors["text"],
                font=("Segoe UI", 9),
            )
            style.map(
                "Setting.TCheckbutton",
                background=[("active", colors["panel_alt"])],
                foreground=[("disabled", colors["muted"])],
            )
            style.configure(
                "ProfileTitle.TLabel",
                background=colors["panel"],
                foreground="#D8D3C8",
                font=("Segoe UI", 10, "bold"),
            )
            style.configure(
                "Value.TLabel",
                background=colors["panel"],
                foreground=colors["cyan"],
                font=("Segoe UI", 10, "bold"),
            )
            style.configure(
                "Dota.TButton",
                background="#171E23",
                foreground=colors["text"],
                bordercolor="#374149",
                lightcolor="#374149",
                darkcolor="#06090B",
                padding=(14, 7),
                font=("Segoe UI", 9, "bold"),
            )
            style.map("Dota.TButton", background=[("active", "#222C33"), ("disabled", "#11161A")])
            style.configure(
                "Mode.TButton",
                background="#11171B",
                foreground=colors["cyan"],
                bordercolor="#374149",
                lightcolor="#374149",
                darkcolor="#06090B",
                padding=(10, 8),
                font=("Segoe UI Semibold", 10),
                anchor="center",
                justify="center",
            )
            style.map("Mode.TButton", background=[("active", "#1B242A"), ("disabled", "#0E1215")])
            style.configure(
                "ModeActive.TButton",
                background="#211511",
                foreground="#F0DDD4",
                bordercolor=colors["red_hover"],
                lightcolor=colors["red_hover"],
                darkcolor="#6B2115",
                padding=(10, 8),
                font=("Segoe UI Semibold", 10),
                anchor="center",
                justify="center",
            )
            style.map(
                "ModeActive.TButton",
                background=[("active", "#2C1A15"), ("disabled", "#17110F")],
            )
            style.configure(
                "Primary.TButton",
                background=colors["green"],
                foreground="#F1F1E8",
                bordercolor="#759A48",
                lightcolor="#759A48",
                darkcolor="#253B18",
                padding=(22, 11),
                font=("Segoe UI Semibold", 11),
            )
            style.map(
                "Primary.TButton",
                background=[("active", colors["green_hover"]), ("disabled", "#293024")],
                foreground=[("disabled", "#777C73")],
            )
            style.configure(
                "TEntry",
                fieldbackground=colors["panel_alt"],
                foreground=colors["text"],
                insertcolor=colors["text"],
                bordercolor=colors["border"],
                lightcolor=colors["border"],
                darkcolor="#030506",
                padding=6,
            )
            style.configure(
                "TCombobox",
                fieldbackground=colors["panel_alt"],
                background=colors["panel_alt"],
                foreground=colors["text"],
                arrowcolor=colors["muted"],
                bordercolor=colors["border"],
                lightcolor=colors["border"],
                darkcolor="#030506",
                padding=7,
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", colors["panel_alt"])],
                foreground=[("readonly", colors["text"])],
                selectbackground=[("readonly", colors["panel_alt"])],
                selectforeground=[("readonly", colors["text"])],
            )
            style.configure("TNotebook", background=colors["panel"], borderwidth=0, tabmargins=(0, 0, 0, 12))
            style.configure(
                "TNotebook.Tab",
                background=colors["top"],
                foreground=colors["muted"],
                borderwidth=0,
                padding=(22, 10),
                font=("Segoe UI", 9, "bold"),
            )
            style.map(
                "TNotebook.Tab",
                background=[("selected", colors["panel"])],
                foreground=[("selected", "#F1E9DC")],
            )
            style.configure(
                "Horizontal.TScale",
                background=colors["panel"],
                troughcolor=colors["panel_alt"],
            )
            style.configure(
                "Horizontal.TProgressbar",
                background=colors["red"],
                troughcolor=colors["panel_alt"],
                borderwidth=0,
                thickness=6,
            )

        def _load_assets(self) -> None:
            self.window_icon_photo = self._load_photo(WINDOW_ICON_FILE, (96, 96))
            if self.window_icon_photo is not None:
                self.root.iconphoto(True, self.window_icon_photo)

            self.header_photo = self._load_photo(HEADER_IMAGE_FILE, (330, 220))
            self.dota_logo_photo = self._load_photo(DOTA_LOGO_FILE, (50, 50))
            self.mode_photos = {
                mode: photo
                for mode, filename in MODE_ICON_FILES.items()
                if (photo := self._load_photo(filename, (56, 56))) is not None
            }

        def _load_photo(self, filename: str, size: tuple[int, int]) -> object | None:
            asset_path = resource_path(filename)
            if not asset_path.exists() or Image is None or ImageOps is None or ImageTk is None:
                return None

            image = Image.open(asset_path)
            resized = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(resized)

        def _section_title(self, parent: object, text: str, row: int) -> None:
            ttk.Label(parent, text=text, style="SectionTitle.TLabel").grid(
                row=row, column=0, sticky="w"
            )

        def _combo(self, parent: object, variable: tk.StringVar, values: tuple[str, ...], width: int) -> ttk.Combobox:
            combo = ttk.Combobox(
                parent, textvariable=variable, values=values, state="readonly", width=width
            )
            combo.bind("<<ComboboxSelected>>", self._on_setting_changed)
            self.interactive_widgets.append(combo)
            return combo

        def _build_ui(self) -> None:
            colors = self.colors
            shell = ttk.Frame(self.root, style="App.TFrame")
            shell.pack(fill="both", expand=True)

            header = ttk.Frame(shell, padding=(24, 8), style="Top.TFrame")
            header.pack(fill="x")
            if self.dota_logo_photo is not None:
                tk.Label(
                    header,
                    image=self.dota_logo_photo,
                    background=colors["top"],
                    borderwidth=0,
                    highlightthickness=0,
                ).pack(side="left")
            ttk.Label(header, text="CUSINI-ROYALE TOOL", style="Title.TLabel").pack(
                side="left", padx=(18, 0)
            )

            red_line = tk.Frame(shell, background=colors["red"], height=2)
            red_line.pack(fill="x")

            main = ttk.Frame(shell, padding=(20, 12), style="App.TFrame")
            main.pack(fill="both", expand=True)
            main.columnconfigure(0, weight=13)
            main.columnconfigure(1, weight=6)
            main.rowconfigure(0, weight=1)

            workspace = ttk.Frame(main, padding=(16, 12), style="Panel.TFrame")
            workspace.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
            workspace.columnconfigure(0, weight=1)

            # 1 · Файл
            self._section_title(workspace, "1 · ФАЙЛ", 0)
            file_panel = ttk.Frame(workspace, padding=12, style="Inset.TFrame")
            file_panel.grid(row=1, column=0, sticky="ew", pady=(6, 10))
            file_panel.columnconfigure(0, weight=1)
            input_entry = ttk.Entry(file_panel, textvariable=self.input_path_var, state="readonly")
            input_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
            browse_button = ttk.Button(
                file_panel,
                text="ВЫБРАТЬ ФАЙЛ",
                command=self.choose_input_file,
                style="Dota.TButton",
            )
            browse_button.grid(row=0, column=1)
            self.interactive_widgets.append(browse_button)
            ttk.Label(
                file_panel,
                textvariable=self.input_info_var,
                style="SettingHint.TLabel",
                wraplength=640,
                justify="left",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

            # 2 · Фрагмент
            self._section_title(workspace, "2 · ФРАГМЕНТ", 2)
            self.trim_panel = ttk.Frame(workspace, padding=(12, 10), style="Inset.TFrame")
            self.trim_panel.grid(row=3, column=0, sticky="ew", pady=(6, 10))
            self._build_trim_panel()

            # 3 · Что сделать
            self._section_title(workspace, "3 · ЧТО СДЕЛАТЬ", 4)
            mode_cards = ttk.Frame(workspace, style="Panel.TFrame")
            mode_cards.grid(row=5, column=0, sticky="ew", pady=(6, 0))
            for column in range(len(self.MODE_SPECS)):
                mode_cards.columnconfigure(column, weight=1, uniform="mode")

            self.mode_buttons: dict[str, ttk.Button] = {}
            last_column = len(self.MODE_SPECS) - 1
            for column, (mode, title, subtitle) in enumerate(self.MODE_SPECS):
                button = ttk.Button(
                    mode_cards,
                    text=f"{title}\n{subtitle}",
                    image=self.mode_photos.get(mode, ""),
                    compound="top",
                    command=lambda selected_mode=mode: self._select_mode(selected_mode),
                    style="Mode.TButton",
                )
                button.grid(
                    row=0,
                    column=column,
                    sticky="nsew",
                    padx=(0 if column == 0 else 4, 0 if column == last_column else 4),
                )
                self.mode_buttons[mode] = button
                self.interactive_widgets.append(button)

            self.settings_host = ttk.Frame(workspace, padding=(14, 10), style="Inset.TFrame")
            self.settings_host.grid(row=6, column=0, sticky="ew", pady=(8, 10))
            self.settings_host.columnconfigure(0, weight=1)
            self.settings_host.rowconfigure(0, minsize=72)
            self.mode_frames = {
                mode: ttk.Frame(self.settings_host, style="Inset.TFrame")
                for mode, _title, _subtitle in self.MODE_SPECS
            }
            self._build_discord_settings()
            self._build_retro_settings()
            self._build_gif_settings()
            self._build_custom_settings()

            # Результат и запуск
            output_row = ttk.Frame(workspace, style="Panel.TFrame")
            output_row.grid(row=7, column=0, sticky="ew", pady=(0, 10))
            output_row.columnconfigure(0, weight=1)
            ttk.Label(
                output_row,
                textvariable=self.output_var,
                style="Hint.TLabel",
                wraplength=520,
                justify="left",
            ).grid(row=0, column=0, sticky="w")
            self.reset_output_button = ttk.Button(
                output_row, text="РЯДОМ С ИСХОДНИКОМ", command=self.reset_output_path, style="Dota.TButton"
            )
            self.reset_output_button.grid(row=0, column=1, padx=(8, 0))
            self.reset_output_button.grid_remove()
            save_as_button = ttk.Button(
                output_row, text="СОХРАНИТЬ КАК…", command=self.choose_output_file, style="Dota.TButton"
            )
            save_as_button.grid(row=0, column=2, padx=(8, 0))
            self.interactive_widgets.extend([self.reset_output_button, save_as_button])

            actions = ttk.Frame(workspace, style="Panel.TFrame")
            actions.grid(row=8, column=0, sticky="ew")
            actions.columnconfigure(0, weight=1)
            self.run_button = ttk.Button(
                actions, text="▶", command=self.start_operation, style="Primary.TButton"
            )
            self.run_button.grid(row=0, column=0, sticky="ew")
            self.interactive_widgets.append(self.run_button)
            self.open_folder_button = ttk.Button(
                actions,
                text="ПОКАЗАТЬ ФАЙЛ",
                command=self.open_output_folder,
                state="disabled",
                style="Dota.TButton",
            )
            self.open_folder_button.grid(row=0, column=1, padx=(10, 0), sticky="ns")

            self._build_sidebar(main)

        def _build_sidebar(self, main: object) -> None:
            colors = self.colors
            sidebar = ttk.Frame(main, padding=16, style="Panel.TFrame")
            sidebar.grid(row=0, column=1, sticky="nsew")
            sidebar.columnconfigure(0, weight=1)
            sidebar.rowconfigure(7, weight=1)

            ttk.Label(sidebar, text="ЗА ДЕЛО БЕРЕТСЯ МЯСНИК", style="SectionTitle.TLabel").grid(
                row=0, column=0, sticky="w"
            )
            if self.header_photo is not None:
                tk.Label(
                    sidebar,
                    image=self.header_photo,
                    background=colors["panel"],
                    borderwidth=1,
                    relief="solid",
                    highlightbackground=colors["border"],
                ).grid(row=1, column=0, sticky="nsew", pady=(10, 8))
            ttk.Label(
                sidebar,
                text="Ой... это я что ли?",
                style="ProfileTitle.TLabel",
                justify="center",
            ).grid(row=2, column=0, sticky="ew", pady=(0, 14))

            ttk.Label(sidebar, text="СТАТУС", style="SectionTitle.TLabel").grid(
                row=3, column=0, sticky="w"
            )
            ttk.Label(
                sidebar,
                textvariable=self.status_var,
                style="Hint.TLabel",
                wraplength=300,
                justify="left",
            ).grid(row=4, column=0, sticky="ew", pady=(8, 10))
            self.progress = ttk.Progressbar(sidebar, mode="determinate", maximum=100)
            self.progress.grid(row=5, column=0, sticky="ew", pady=(0, 14))

            ttk.Label(sidebar, text="ЖУРНАЛ", style="SectionTitle.TLabel").grid(
                row=6, column=0, sticky="w", pady=(0, 8)
            )
            log_frame = ttk.Frame(sidebar, style="Inset.TFrame")
            log_frame.grid(row=7, column=0, sticky="nsew")
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)
            self.log_text = tk.Text(
                log_frame,
                width=32,
                height=6,
                wrap="word",
                state="disabled",
                font=("Cascadia Mono", 9),
                background=colors["panel_alt"],
                foreground="#A3A7A5",
                insertbackground=colors["text"],
                selectbackground=colors["red"],
                relief="flat",
                borderwidth=0,
                padx=12,
                pady=12,
            )
            self.log_text.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.log_text.configure(yscrollcommand=scrollbar.set)

        def _build_trim_panel(self) -> None:
            panel = self.trim_panel
            panel.columnconfigure(1, weight=1)

            for row, (label, scale_var, entry_var, handler) in enumerate(
                (
                    ("Начало:", self.trim_start_scale_var, self.trim_start_var, self._on_trim_start_scale),
                    ("Конец:", self.trim_end_scale_var, self.trim_end_var, self._on_trim_end_scale),
                )
            ):
                ttk.Label(panel, text=label, style="Setting.TLabel").grid(
                    row=row, column=0, sticky="w", pady=(0, 6)
                )
                scale = ttk.Scale(
                    panel, from_=0, to=self.trim_duration, variable=scale_var, command=handler
                )
                scale.grid(row=row, column=1, sticky="ew", padx=12, pady=(0, 6))
                entry = ttk.Entry(panel, textvariable=entry_var, width=9)
                entry.grid(row=row, column=2, sticky="e", pady=(0, 6))
                entry.bind("<FocusOut>", self._on_trim_entry_changed)
                entry.bind("<Return>", self._on_trim_entry_changed)
                ttk.Label(panel, text="сек", style="SettingHint.TLabel").grid(
                    row=row, column=3, sticky="w", padx=(6, 0), pady=(0, 6)
                )
                self.interactive_widgets.extend([scale, entry])
                if row == 0:
                    self.trim_start_scale = scale
                else:
                    self.trim_end_scale = scale

            ttk.Label(
                panel, textvariable=self.trim_selection_var, style="SettingValue.TLabel"
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
            full_button = ttk.Button(
                panel, text="ВСЁ ВИДЕО", command=self.select_full_range, style="Dota.TButton"
            )
            full_button.grid(row=2, column=2, columnspan=2, sticky="e", pady=(2, 0))
            self.interactive_widgets.append(full_button)

        def _build_discord_settings(self) -> None:
            frame = self.mode_frames["discord"]
            frame.columnconfigure(2, weight=1)
            ttk.Label(frame, text="Лимит:", style="Setting.TLabel").grid(row=0, column=0, sticky="w")
            self._combo(frame, self.limit_var, SIZE_LIMIT_OPTIONS, 8).grid(
                row=0, column=1, sticky="w", padx=(8, 14)
            )
            ttk.Label(
                frame,
                textvariable=self.plan_var,
                style="SettingValue.TLabel",
                wraplength=520,
                justify="left",
            ).grid(row=0, column=2, sticky="w")
            ttk.Label(
                frame,
                text="Битрейт и разрешение подбираются под длину фрагмента. 10 МБ — Discord без Nitro.",
                style="SettingHint.TLabel",
                wraplength=720,
                justify="left",
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        def _build_retro_settings(self) -> None:
            frame = self.mode_frames["retro"]
            frame.columnconfigure(2, weight=1)
            ttk.Label(frame, text="Лимит:", style="Setting.TLabel").grid(row=0, column=0, sticky="w")
            self._combo(frame, self.limit_var, SIZE_LIMIT_OPTIONS, 8).grid(
                row=0, column=1, sticky="w", padx=(8, 14)
            )
            ttk.Label(
                frame,
                text="ШАКАЛЬНАЯ ЗАПИСЬ РАБОЧЕГО СТОЛА",
                style="SettingTitle.TLabel",
            ).grid(row=0, column=2, sticky="w")
            ttk.Label(
                frame,
                text=(
                    f"Кроп по центру в 4:3 · {RETRO_WIDTH}×{RETRO_HEIGHT} · {RETRO_FPS} FPS · "
                    "звук +20% · watermark Bandicam. Не влезет — дошакалим."
                ),
                style="SettingHint.TLabel",
                wraplength=620,
                justify="left",
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        def _build_gif_settings(self) -> None:
            frame = self.mode_frames["gif"]
            frame.columnconfigure(5, weight=1)
            fields = (
                ("Ширина:", self.gif_width_var, ("Исходная", "720 px", "480 px", "320 px", "240 px")),
                ("Кадр:", self.gif_aspect_var, tuple(GIF_ASPECTS)),
                ("FPS:", self.gif_fps_var, ("25", "20", "15", "12", "10", "8")),
                ("Цвета:", self.gif_colors_var, ("256", "128", "96", "64", "32")),
            )
            for index, (label, variable, values) in enumerate(fields):
                row, pair = divmod(index, 2)
                column = pair * 2
                ttk.Label(frame, text=label, style="Setting.TLabel").grid(
                    row=row,
                    column=column,
                    sticky="w",
                    padx=(0 if column == 0 else 22, 8),
                    pady=(0 if row == 0 else 8, 0),
                )
                self._combo(frame, variable, values, 10).grid(
                    row=row, column=column + 1, sticky="w", pady=(0 if row == 0 else 8, 0)
                )

            ttk.Label(frame, text="Макс. вес:", style="Setting.TLabel").grid(
                row=0, column=4, sticky="w", padx=(22, 8)
            )
            target_entry = ttk.Entry(frame, textvariable=self.gif_target_mb_var, width=6)
            target_entry.grid(row=0, column=5, sticky="w")
            self.interactive_widgets.append(target_entry)
            ttk.Label(
                frame,
                text="МБ, необязательно — снизим FPS и цвета",
                style="SettingHint.TLabel",
            ).grid(row=1, column=4, columnspan=2, sticky="w", padx=(22, 0), pady=(8, 0))

        def _build_custom_settings(self) -> None:
            frame = self.mode_frames["custom"]
            frame.columnconfigure(2, weight=1)

            ttk.Label(frame, text="Пресет:", style="Setting.TLabel").grid(row=0, column=0, sticky="w")
            profile_combo = self._combo(
                frame,
                self.profile_var,
                tuple(profile.title for profile in COMPRESSION_PROFILES if not profile.retro_square),
                22,
            )
            profile_combo.grid(row=0, column=1, sticky="w", padx=(8, 12))
            ttk.Label(
                frame,
                textvariable=self.profile_description_var,
                style="SettingHint.TLabel",
                wraplength=340,
                justify="left",
            ).grid(row=0, column=2, sticky="w")

            resize_row = ttk.Frame(frame, style="Inset.TFrame")
            resize_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            ttk.Label(resize_row, text="Размер:", style="Setting.TLabel").grid(
                row=0, column=0, sticky="w"
            )
            self._combo(resize_row, self.resize_mode_var, ("Как в пресете", "Задать вручную"), 15).grid(
                row=0, column=1, padx=(8, 12)
            )
            self.resize_width_entry = ttk.Entry(
                resize_row, textvariable=self.resize_width_var, width=7, state="disabled"
            )
            self.resize_width_entry.grid(row=0, column=2)
            ttk.Label(resize_row, text="×", style="Setting.TLabel").grid(row=0, column=3, padx=6)
            self.resize_height_entry = ttk.Entry(
                resize_row, textvariable=self.resize_height_var, width=7, state="disabled"
            )
            self.resize_height_entry.grid(row=0, column=4)
            ttk.Label(resize_row, text="px", style="Setting.TLabel").grid(
                row=0, column=5, sticky="w", padx=(6, 12)
            )
            self.keep_aspect_check = ttk.Checkbutton(
                resize_row,
                text="Сохранять пропорции",
                variable=self.keep_aspect_var,
                state="disabled",
                style="Setting.TCheckbutton",
            )
            self.keep_aspect_check.grid(row=0, column=6, sticky="w")
            self.interactive_widgets.extend(
                [self.resize_width_entry, self.resize_height_entry, self.keep_aspect_check]
            )

        # ---- состояние ----

        def _active_mode(self) -> str:
            return self.mode_var.get()

        def _select_mode(self, mode: str) -> None:
            self.mode_var.set(mode)
            for frame in self.mode_frames.values():
                frame.grid_remove()
            self.mode_frames[mode].grid(row=0, column=0, sticky="ew")
            for button_mode, button in self.mode_buttons.items():
                button.configure(
                    style="ModeActive.TButton" if button_mode == mode else "Mode.TButton"
                )
            self.run_button.configure(text=self.RUN_LABELS[mode])
            self._refresh_derived()

        def _on_setting_changed(self, _event: object | None = None) -> None:
            self._refresh_profile_state()
            self._apply_resize_state()
            self._refresh_derived()

        def _apply_resize_state(self) -> None:
            manual = self.resize_mode_var.get() == "Задать вручную"
            state = "normal" if manual and not self.is_busy else "disabled"
            self.resize_width_entry.configure(state=state)
            self.resize_height_entry.configure(state=state)
            self.keep_aspect_check.configure(state=state)

        def _refresh_profile_state(self) -> None:
            profile = COMPRESSION_PROFILE_BY_TITLE[self.profile_var.get()]
            self.profile_description_var.set(profile.description)

        def _refresh_derived(self) -> None:
            self._refresh_plan()
            self._refresh_output_label()

        def _selected_range(self) -> tuple[float, float | None]:
            start = self.trim_start_scale_var.get()
            end = self.trim_end_scale_var.get()
            duration_known = self.media_info is not None and self.media_info.duration
            if duration_known and end >= self.trim_duration - 0.05:
                return start, None
            return start, end

        def _refresh_plan(self) -> None:
            if self.selected_input is None or self.media_info is None:
                self.plan_var.set("Выберите файл — здесь появится расчёт битрейта.")
                return
            if not self.media_info.duration:
                self.plan_var.set("Длительность не определилась — битрейт посчитаем при запуске.")
                return
            start, end = self._selected_range()
            duration = (end if end is not None else self.trim_duration) - start
            try:
                limit = parse_size_limit(self.limit_var.get())
                plan = plan_bitrate(
                    duration,
                    limit,
                    has_audio=self.media_info.has_audio,
                    info=self.media_info,
                )
            except OperationError as exc:
                self.plan_var.set(f"⚠ {exc}")
                return
            self.plan_var.set(f"{format_duration(duration)} → {describe_plan(plan, self.media_info)}")

        def _planned_output(self) -> Path | None:
            if self.selected_input is None:
                return None
            mode = self._active_mode()
            return resolve_output_path(
                self.selected_input,
                self.custom_output,
                export_output_tag(mode, self.selected_input),
                output_suffix=export_output_suffix(mode),
            )

        def _refresh_output_label(self) -> None:
            output = self._planned_output()
            if output is None:
                self.output_var.set("Результат сохранится рядом с исходным файлом.")
                self.reset_output_button.grid_remove()
                return
            if self.custom_output is None:
                self.output_var.set(f"Сохранится как  {output.name}  (рядом с исходником)")
                self.reset_output_button.grid_remove()
            else:
                self.output_var.set(f"Сохранится как  {output}")
                self.reset_output_button.grid()

        # ---- фрагмент ----

        def _set_trim_duration(self, duration: float | None) -> None:
            self.trim_duration = max(duration or DEFAULT_TRIM_DURATION, 0.2)
            self.trim_start_scale.configure(to=self.trim_duration)
            self.trim_end_scale.configure(to=self.trim_duration)
            self._set_trim_values(0.0, self.trim_duration)

        def select_full_range(self) -> None:
            self._set_trim_values(0.0, self.trim_duration)

        def _set_trim_values(self, start: float, end: float) -> None:
            start = max(0.0, min(start, self.trim_duration))
            end = max(0.0, min(end, self.trim_duration))
            if end <= start:
                end = min(self.trim_duration, start + 0.1)
            if end <= start:
                start = max(0.0, end - 0.1)

            self._syncing_trim_controls = True
            self.trim_start_scale_var.set(start)
            self.trim_end_scale_var.set(end)
            self.trim_start_var.set(format_seconds(round(start, 2)))
            self.trim_end_var.set(format_seconds(round(end, 2)))
            self._syncing_trim_controls = False

            length = end - start
            if start <= 0 and end >= self.trim_duration - 0.05:
                self.trim_selection_var.set(f"Всё видео · {format_duration(length)}")
            else:
                self.trim_selection_var.set(
                    f"Фрагмент: {format_duration(length)} из {format_duration(self.trim_duration)}"
                )
            self._refresh_plan()

        def _on_trim_start_scale(self, raw_value: str) -> None:
            if self._syncing_trim_controls:
                return
            start = round(float(raw_value), 1)
            end = self.trim_end_scale_var.get()
            if start >= end:
                end = min(self.trim_duration, start + 0.1)
            self._set_trim_values(start, end)

        def _on_trim_end_scale(self, raw_value: str) -> None:
            if self._syncing_trim_controls:
                return
            end = round(float(raw_value), 1)
            start = self.trim_start_scale_var.get()
            if end <= start:
                start = max(0.0, end - 0.1)
            self._set_trim_values(start, end)

        def _on_trim_entry_changed(self, _event: object | None = None) -> None:
            if self._syncing_trim_controls:
                return
            try:
                start = self._parse_non_negative_float(self.trim_start_var.get(), "Начало")
                end = self._parse_non_negative_float(self.trim_end_var.get(), "Конец")
            except ValueError:
                return
            self._set_trim_values(start, end)

        # ---- файлы ----

        def _video_dialog_types(self) -> list[tuple[str, str]]:
            mask = " ".join(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS))
            return [("Видео и GIF", mask), ("GIF-анимации", "*.gif"), ("Все файлы", "*.*")]

        def choose_input_file(self) -> None:
            file_path = filedialog.askopenfilename(
                parent=self.root,
                title="Выберите видео или GIF",
                filetypes=self._video_dialog_types(),
            )
            if file_path:
                self.load_input_file(Path(file_path))

        def load_input_file(self, path: Path) -> None:
            self.selected_input = ensure_input_exists(path)
            self.media_info = probe_media(self.selected_input)
            info = self.media_info
            self.input_path_var.set(str(self.selected_input))

            parts = [format_size(self.selected_input.stat().st_size)]
            if info.duration:
                parts.append(format_duration(info.duration))
            if info.width and info.height:
                parts.append(f"{info.width}×{info.height}")
            if info.fps:
                parts.append(f"{format_seconds(round(info.fps, 2))} FPS")
            parts.append("со звуком" if info.has_audio else "без звука")
            self.input_info_var.set(" · ".join(parts))

            self.custom_output = None
            self.last_output_path = None
            self.open_folder_button.configure(state="disabled")
            self.progress.configure(value=0)
            self._set_trim_duration(info.duration)
            if self.selected_input.suffix.lower() == ".gif":
                self._select_mode("gif")
            else:
                self._refresh_derived()
            self.status_var.set("Файл выбран. Выделите фрагмент и жмите кнопку.")

        def reset_output_path(self) -> None:
            self.custom_output = None
            self._refresh_output_label()

        def choose_output_file(self) -> None:
            planned = self._planned_output()
            suffix = export_output_suffix(self._active_mode())
            kind = "GIF" if suffix == ".gif" else "MP4"
            output_path = filedialog.asksaveasfilename(
                parent=self.root,
                title="Куда сохранить результат",
                filetypes=[(kind, f"*{suffix}"), ("Все файлы", "*.*")],
                defaultextension=suffix,
                initialdir=str(planned.parent if planned else Path.cwd()),
                initialfile=planned.name if planned else "",
            )
            if output_path:
                self.custom_output = Path(output_path)
                self._refresh_output_label()

        def _parse_non_negative_float(self, raw_value: str, field_name: str) -> float:
            value_text = raw_value.strip().replace(",", ".")
            if not value_text:
                raise ValueError(f"Укажите значение поля: {field_name}.")
            try:
                value = float(value_text)
            except ValueError as exc:
                raise ValueError(f"Поле '{field_name}' должно быть числом.") from exc
            if value < 0:
                raise ValueError(f"Поле '{field_name}' не может быть отрицательным.")
            return value

        def _parse_optional_dimension(self, raw_value: str, field_name: str) -> int | None:
            value_text = raw_value.strip()
            if not value_text:
                return None
            try:
                value = int(value_text)
            except ValueError as exc:
                raise ValueError(f"Поле '{field_name}' должно быть целым числом.") from exc
            if value <= 0:
                raise ValueError(f"Поле '{field_name}' должно быть больше нуля.") from None
            return value

        def _build_job(self) -> ExportJob:
            if self.selected_input is None:
                raise ValueError("Сначала выберите файл.")
            self._on_trim_entry_changed()
            start, end = self._selected_range()
            mode = self._active_mode()
            job = ExportJob(
                input_path=self.selected_input,
                mode=mode,
                output_path=self.custom_output,
                start=start,
                end=end,
                size_limit_mb=parse_size_limit(self.limit_var.get()),
            )

            if mode == "gif":
                width_text = self.gif_width_var.get()
                job.gif_width = None if width_text == "Исходная" else int(width_text.split()[0])
                job.gif_aspect = self.gif_aspect_var.get()
                job.gif_fps = int(self.gif_fps_var.get())
                job.gif_colors = int(self.gif_colors_var.get())
                target_text = self.gif_target_mb_var.get().strip().replace(",", ".")
                if target_text:
                    try:
                        job.gif_target_mb = float(target_text)
                    except ValueError as exc:
                        raise ValueError("Макс. вес GIF должен быть числом в мегабайтах.") from exc
                    if job.gif_target_mb <= 0:
                        raise ValueError("Макс. вес GIF должен быть больше нуля.")
            elif mode == "custom":
                job.profile_title = self.profile_var.get()
                if self.resize_mode_var.get() == "Задать вручную":
                    job.width = self._parse_optional_dimension(self.resize_width_var.get(), "Ширина")
                    job.height = self._parse_optional_dimension(self.resize_height_var.get(), "Высота")
                    if job.width is None and job.height is None:
                        raise ValueError("Укажите ширину или высоту итогового файла.")
                    job.keep_aspect = self.keep_aspect_var.get()
                    if not job.keep_aspect and (job.width is None or job.height is None):
                        raise ValueError(
                            "Для растягивания без сохранения пропорций укажите и ширину, и высоту."
                        )
            return job

        # ---- запуск ----

        def start_operation(self) -> None:
            if self.worker_thread and self.worker_thread.is_alive():
                return
            try:
                job = self._build_job()
            except ValueError as exc:
                messagebox.showerror("Ошибка", str(exc), parent=self.root)
                self.status_var.set(str(exc))
                return

            start_message = {
                "discord": "Сжимаю под Discord…",
                "retro": "Шакалю в стиле 2000-х…",
                "gif": "Делаю GIF…",
                "custom": "Сжимаю…",
            }[job.mode]
            self.clear_log()
            self.append_log(start_message)
            planned = self._planned_output()
            if planned is not None:
                self.append_log(f"Файл результата: {planned}")
            self.progress.configure(value=0)
            self._set_busy(True, start_message)

            self.worker_thread = threading.Thread(
                target=self._run_worker, args=(job,), daemon=True
            )
            self.worker_thread.start()

        def _report_progress(self, fraction: float) -> None:
            self.root.after(0, self._show_progress, fraction)

        def _show_progress(self, fraction: float) -> None:
            if not self.is_busy:
                return
            percent = int(fraction * 100)
            self.progress.configure(value=percent)
            self.status_var.set(f"Обработка… {percent}%")

        def _run_worker(self, job: ExportJob) -> None:
            try:
                result = process_export(job, on_progress=self._report_progress)
            except OperationError as exc:
                self.root.after(0, self._finish_with_error, exc)
                return
            except Exception as exc:
                self.root.after(
                    0, self._finish_with_error, OperationError(f"Неожиданная ошибка: {exc}")
                )
                return
            self.root.after(0, self._finish_with_success, result)

        def _finish_with_success(self, result: OperationResult) -> None:
            size = format_size(result.output_path.stat().st_size)
            self._set_busy(False, f"Готово: {result.output_path.name} — {size}")
            self.progress.configure(value=100)
            self.last_output_path = result.output_path
            self.open_folder_button.configure(state="normal")
            self.append_log("")
            for line in result.details:
                self.append_log(line)

        def _finish_with_error(self, error: OperationError) -> None:
            self._set_busy(False, "Операция завершилась с ошибкой.")
            self.progress.configure(value=0)
            self.append_log("")
            self.append_log(str(error))
            messagebox.showerror("Ошибка", str(error), parent=self.root)

        def _set_busy(self, busy: bool, status_text: str) -> None:
            self.is_busy = busy
            self.status_var.set(status_text)
            for widget in self.interactive_widgets:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="disabled" if busy else "readonly")
                else:
                    widget.configure(state="disabled" if busy else "normal")
            if not busy:
                self._apply_resize_state()

            if not busy and self.last_output_path is not None and self.last_output_path.exists():
                self.open_folder_button.configure(state="normal")
            else:
                self.open_folder_button.configure(state="disabled")

        def append_log(self, message: str) -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"{message}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def clear_log(self) -> None:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

        def open_output_folder(self) -> None:
            if self.last_output_path is None:
                return

            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(self.last_output_path)])
                return

            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(self.last_output_path)])
                return

            subprocess.Popen(["xdg-open", str(self.last_output_path.parent)])


def launch_gui() -> int:
    if tk is None or ttk is None or filedialog is None or messagebox is None:
        print(
            "Графический интерфейс недоступен: в установленном Python нет tkinter.",
            file=sys.stderr,
        )
        return 1
    if Image is None or ImageOps is None or ImageTk is None:
        print(
            "Графический интерфейс недоступен: установите зависимости из requirements.txt, включая Pillow.",
            file=sys.stderr,
        )
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Не удалось запустить графический интерфейс: {exc}", file=sys.stderr)
        return 1

    VideoToolApp(root)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        return launch_gui()

    known_commands = {"gui", "menu", "compress", "trim", "export", "-h", "--help"}
    if argv[0] not in known_commands:
        argv = ["compress", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "gui":
        return launch_gui()
    if args.command in {None, "menu"}:
        return run_menu()
    if args.command == "compress":
        return run_compress(args)
    if args.command == "trim":
        return run_trim(args)
    if args.command == "export":
        return run_export(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
