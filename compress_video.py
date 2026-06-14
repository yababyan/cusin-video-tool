from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

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


APP_TITLE = "CUSINI ROYAL VIDEO TOOL"
HEADER_IMAGE_FILE = "images.jpg"
WINDOW_ICON_FILE = "DotA2MinimapIcons_AgADagwAAsd2IVA.png"
EXE_ICON_FILE = "cusini_royal_video_tool.ico"
DEFAULT_TRIM_DURATION = 60.0


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


def resolve_output_path(input_path: Path, output_path: Path | None, suffix: str) -> Path:
    if output_path:
        return normalize_output_path(output_path)
    return input_path.with_name(f"{input_path.stem}_{suffix}.mp4")


def normalize_output_path(output_path: Path) -> Path:
    normalized = output_path.expanduser()
    if not normalized.is_absolute():
        normalized = Path.cwd() / normalized
    if not normalized.suffix:
        normalized = normalized.with_suffix(".mp4")
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


def get_ffprobe_executable() -> str | None:
    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        return system_ffprobe

    try:
        ffmpeg_path = Path(get_ffmpeg_executable())
    except RuntimeError:
        return None

    candidate_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    candidate = ffmpeg_path.with_name(candidate_name)
    if candidate.exists():
        return str(candidate)
    return None


def probe_video_duration(input_path: Path) -> float | None:
    ffprobe = get_ffprobe_executable()
    if ffprobe is None:
        return None

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]

    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None

    if duration <= 0:
        return None
    return duration


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
        "-c:v",
        "libx264",
        "-preset",
        args.preset,
        "-crf",
        str(args.crf),
    ]

    if args.width:
        command.extend(["-vf", f"scale={args.width}:-2"])

    if args.no_audio:
        command.append("-an")
    else:
        command.extend(["-c:a", "aac", "-b:a", args.audio_bitrate])

    command.extend(["-movflags", "+faststart", str(output_path)])
    return command


def build_trim_command(input_path: Path, output_path: Path, start: float, end: float) -> list[str]:
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
    return normalize_output_path(Path(raw_value.strip('"')))


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
            print("Видео в текущей папке не найдены.")

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
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title(APP_TITLE)
            self.root.geometry("980x720")
            self.root.minsize(880, 660)

            self.selected_input: Path | None = None
            self.last_output_path: Path | None = None
            self.auto_output_path: str | None = None
            self.worker_thread: threading.Thread | None = None
            self.is_busy = False
            self.header_photo: object | None = None
            self.window_icon_photo: object | None = None
            self.trim_duration = DEFAULT_TRIM_DURATION
            self._syncing_trim_controls = False

            self.input_path_var = tk.StringVar()
            self.input_info_var = tk.StringVar(value="Файл не выбран.")
            self.output_path_var = tk.StringVar()
            self.status_var = tk.StringVar(value="Выберите видеофайл и нужное действие.")
            self.profile_var = tk.StringVar(value=COMPRESSION_PROFILES[1].title)
            self.profile_description_var = tk.StringVar()
            self.trim_start_var = tk.StringVar(value="0")
            self.trim_end_var = tk.StringVar(value="10")
            self.trim_start_scale_var = tk.DoubleVar(value=0)
            self.trim_end_scale_var = tk.DoubleVar(value=10)
            self.trim_duration_var = tk.StringVar(
                value=f"Диапазон ползунков: 0-{format_seconds(DEFAULT_TRIM_DURATION)} сек"
            )

            self.interactive_widgets: list[object] = []

            self._configure_style()
            self._load_assets()
            self._build_ui()
            self._refresh_profile_state()
            self._update_run_button_text()

        def _configure_style(self) -> None:
            style = ttk.Style()
            for theme_name in ("vista", "xpnative", "clam"):
                if theme_name in style.theme_names():
                    style.theme_use(theme_name)
                    break

            style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
            style.configure("Hint.TLabel", foreground="#505050")
            style.configure("Section.TLabelframe", padding=12)
            style.configure("ProfileTitle.TLabel", font=("Segoe UI", 11, "bold"))
            style.configure("Value.TLabel", font=("Segoe UI", 10, "bold"))

        def _load_assets(self) -> None:
            self.window_icon_photo = self._load_photo(WINDOW_ICON_FILE, (96, 96))
            if self.window_icon_photo is not None:
                self.root.iconphoto(True, self.window_icon_photo)

            self.header_photo = self._load_photo(HEADER_IMAGE_FILE, (279, 181))

        def _load_photo(self, filename: str, size: tuple[int, int]) -> object | None:
            asset_path = resource_path(filename)
            if not asset_path.exists() or Image is None or ImageOps is None or ImageTk is None:
                return None

            image = Image.open(asset_path)
            resized = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(resized)

        def _build_ui(self) -> None:
            main = ttk.Frame(self.root, padding=16)
            main.pack(fill="both", expand=True)

            header = ttk.Frame(main)
            header.pack(fill="x")
            header.columnconfigure(0, weight=1)

            title_block = ttk.Frame(header)
            title_block.grid(row=0, column=0, sticky="nw", padx=(0, 16))
            ttk.Label(title_block, text=APP_TITLE, style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                title_block,
                text="Выберите файл через стандартный проводник Windows и запускайте сжатие или обрезку одной кнопкой.",
                style="Hint.TLabel",
                wraplength=560,
                justify="left",
            ).pack(anchor="w", pady=(6, 0))

            if self.header_photo is not None:
                ttk.Label(header, image=self.header_photo).grid(row=0, column=1, sticky="ne")
            else:
                ttk.Label(
                    header,
                    text="CUSINI\nROYAL",
                    style="ProfileTitle.TLabel",
                    justify="center",
                    anchor="center",
                    width=18,
                ).grid(row=0, column=1, sticky="ne")

            file_frame = ttk.LabelFrame(main, text="Исходный файл", style="Section.TLabelframe")
            file_frame.pack(fill="x", pady=(16, 12))
            file_frame.columnconfigure(0, weight=1)

            input_entry = ttk.Entry(file_frame, textvariable=self.input_path_var, state="readonly")
            input_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

            browse_button = ttk.Button(file_frame, text="Выбрать файл...", command=self.choose_input_file)
            browse_button.grid(row=0, column=1, sticky="ew")
            self.interactive_widgets.append(browse_button)

            ttk.Label(
                file_frame,
                textvariable=self.input_info_var,
                style="Hint.TLabel",
                wraplength=860,
                justify="left",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

            self.notebook = ttk.Notebook(main)
            self.notebook.pack(fill="x")
            self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

            self.compress_tab = ttk.Frame(self.notebook, padding=12)
            self.trim_tab = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(self.compress_tab, text="Сжатие")
            self.notebook.add(self.trim_tab, text="Обрезка")

            self._build_compress_tab()
            self._build_trim_tab()

            output_frame = ttk.LabelFrame(main, text="Результат", style="Section.TLabelframe")
            output_frame.pack(fill="x", pady=(12, 12))
            output_frame.columnconfigure(0, weight=1)

            output_entry = ttk.Entry(output_frame, textvariable=self.output_path_var)
            output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
            self.interactive_widgets.append(output_entry)

            auto_button = ttk.Button(output_frame, text="Авто", command=self.use_auto_output_path)
            auto_button.grid(row=0, column=1, sticky="ew", padx=(0, 8))
            self.interactive_widgets.append(auto_button)

            output_button = ttk.Button(output_frame, text="Выбрать...", command=self.choose_output_file)
            output_button.grid(row=0, column=2, sticky="ew")
            self.interactive_widgets.append(output_button)

            ttk.Label(
                output_frame,
                text="Можно оставить автогенерацию имени файла или выбрать путь вручную.",
                style="Hint.TLabel",
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

            actions = ttk.Frame(main)
            actions.pack(fill="x", pady=(4, 12))

            self.run_button = ttk.Button(actions, text="Запустить", command=self.start_operation)
            self.run_button.pack(side="left")
            self.interactive_widgets.append(self.run_button)

            self.open_folder_button = ttk.Button(
                actions,
                text="Открыть папку результата",
                command=self.open_output_folder,
                state="disabled",
            )
            self.open_folder_button.pack(side="left", padx=(8, 0))

            ttk.Label(
                actions,
                textvariable=self.status_var,
                style="Hint.TLabel",
                wraplength=520,
                justify="right",
            ).pack(side="right")

            progress_frame = ttk.Frame(main)
            progress_frame.pack(fill="x", pady=(0, 12))
            self.progress = ttk.Progressbar(progress_frame, mode="indeterminate")
            self.progress.pack(fill="x")

            log_frame = ttk.LabelFrame(main, text="Журнал", style="Section.TLabelframe")
            log_frame.pack(fill="both", expand=True)
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)

            self.log_text = tk.Text(
                log_frame,
                height=12,
                wrap="word",
                state="disabled",
                font=("Consolas", 10),
            )
            self.log_text.grid(row=0, column=0, sticky="nsew")

            scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.log_text.configure(yscrollcommand=scrollbar.set)

        def _build_compress_tab(self) -> None:
            self.compress_tab.columnconfigure(1, weight=1)

            ttk.Label(self.compress_tab, text="Пресет:").grid(row=0, column=0, sticky="w")
            profile_combo = ttk.Combobox(
                self.compress_tab,
                textvariable=self.profile_var,
                values=[profile.title for profile in COMPRESSION_PROFILES],
                state="readonly",
            )
            profile_combo.grid(row=0, column=1, sticky="ew")
            profile_combo.bind("<<ComboboxSelected>>", self._on_profile_changed)
            self.interactive_widgets.append(profile_combo)

            ttk.Label(
                self.compress_tab,
                textvariable=self.profile_description_var,
                style="ProfileTitle.TLabel",
                wraplength=760,
                justify="left",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 6))

            ttk.Label(
                self.compress_tab,
                text="Процент сжатия примерный и зависит от исходного видео. Уже сильно сжатые ролики могут уменьшиться слабее.",
                style="Hint.TLabel",
                wraplength=760,
                justify="left",
            ).grid(row=2, column=0, columnspan=2, sticky="w")

        def _build_trim_tab(self) -> None:
            self.trim_tab.columnconfigure(1, weight=1)

            ttk.Label(self.trim_tab, text="Начало:").grid(row=0, column=0, sticky="w", pady=(0, 8))
            start_scale = ttk.Scale(
                self.trim_tab,
                from_=0,
                to=self.trim_duration,
                variable=self.trim_start_scale_var,
                command=self._on_trim_start_scale,
            )
            start_scale.grid(row=0, column=1, sticky="ew", padx=(12, 12), pady=(0, 8))
            self.trim_start_scale = start_scale
            self.interactive_widgets.append(start_scale)

            start_entry = ttk.Entry(self.trim_tab, textvariable=self.trim_start_var, width=10)
            start_entry.grid(row=0, column=2, sticky="e", pady=(0, 8))
            start_entry.bind("<FocusOut>", self._on_trim_entry_changed)
            start_entry.bind("<Return>", self._on_trim_entry_changed)
            self.interactive_widgets.append(start_entry)

            ttk.Label(self.trim_tab, text="Конец:").grid(row=1, column=0, sticky="w", pady=(0, 8))
            end_scale = ttk.Scale(
                self.trim_tab,
                from_=0,
                to=self.trim_duration,
                variable=self.trim_end_scale_var,
                command=self._on_trim_end_scale,
            )
            end_scale.grid(row=1, column=1, sticky="ew", padx=(12, 12), pady=(0, 8))
            self.trim_end_scale = end_scale
            self.interactive_widgets.append(end_scale)

            end_entry = ttk.Entry(self.trim_tab, textvariable=self.trim_end_var, width=10)
            end_entry.grid(row=1, column=2, sticky="e", pady=(0, 8))
            end_entry.bind("<FocusOut>", self._on_trim_entry_changed)
            end_entry.bind("<Return>", self._on_trim_entry_changed)
            self.interactive_widgets.append(end_entry)

            ttk.Label(
                self.trim_tab,
                textvariable=self.trim_duration_var,
                style="Value.TLabel",
            ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

            ttk.Label(
                self.trim_tab,
                text="Двигайте ползунки для быстрой обрезки. Поля справа можно использовать для точного значения в секундах.",
                style="Hint.TLabel",
                wraplength=760,
                justify="left",
            ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        def _on_profile_changed(self, _event: object | None = None) -> None:
            self._refresh_profile_state()

        def _refresh_profile_state(self) -> None:
            selected_title = self.profile_var.get()
            profile = COMPRESSION_PROFILE_BY_TITLE[selected_title]
            self.profile_description_var.set(profile.description)

        def _set_trim_duration(self, duration: float | None) -> None:
            self.trim_duration = max(duration or DEFAULT_TRIM_DURATION, 1.0)
            self.trim_start_scale.configure(to=self.trim_duration)
            self.trim_end_scale.configure(to=self.trim_duration)

            start = min(self.trim_start_scale_var.get(), self.trim_duration)
            end = min(max(self.trim_end_scale_var.get(), start + 1), self.trim_duration)
            if end <= start:
                start = 0
                end = min(10.0, self.trim_duration)

            self.trim_duration_var.set(
                f"Диапазон ползунков: 0-{format_seconds(self.trim_duration)} сек"
            )
            self._set_trim_values(start, end)

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
            self.trim_start_var.set(format_seconds(start))
            self.trim_end_var.set(format_seconds(end))
            self._syncing_trim_controls = False

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

        def _on_tab_changed(self, _event: object | None = None) -> None:
            self._update_run_button_text()
            self._update_auto_output_path()

        def _update_run_button_text(self) -> None:
            if self._active_mode() == "compress":
                self.run_button.configure(text="Сжать видео")
            else:
                self.run_button.configure(text="Обрезать видео")

        def _active_mode(self) -> str:
            current_tab = self.notebook.select()
            return "compress" if current_tab == str(self.compress_tab) else "trim"

        def _video_dialog_types(self) -> list[tuple[str, str]]:
            mask = " ".join(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS))
            return [("Видео", mask), ("Все файлы", "*.*")]

        def choose_input_file(self) -> None:
            file_path = filedialog.askopenfilename(
                parent=self.root,
                title="Выберите видеофайл",
                filetypes=self._video_dialog_types(),
            )
            if not file_path:
                return

            self.selected_input = ensure_input_exists(Path(file_path))
            duration = probe_video_duration(self.selected_input)
            self.input_path_var.set(str(self.selected_input))
            duration_text = (
                f" | длительность: {format_seconds(duration)} сек"
                if duration is not None
                else ""
            )
            self.input_info_var.set(
                f"{self.selected_input.name} | {format_size(self.selected_input.stat().st_size)}{duration_text}"
            )
            self._set_trim_duration(duration)
            self.last_output_path = None
            self.open_folder_button.configure(state="disabled")
            self._update_auto_output_path(force=True)
            self.status_var.set("Файл выбран. Можно запускать обработку.")

        def _update_auto_output_path(self, force: bool = False) -> None:
            if self.selected_input is None:
                return

            suffix = "compressed" if self._active_mode() == "compress" else "trimmed"
            suggested = str(resolve_output_path(self.selected_input, None, suffix))
            current = self.output_path_var.get().strip()

            if force or not current or current == self.auto_output_path:
                self.output_path_var.set(suggested)

            self.auto_output_path = suggested

        def use_auto_output_path(self) -> None:
            self._update_auto_output_path(force=True)

        def choose_output_file(self) -> None:
            initial_file = ""
            initial_dir = str(Path.cwd())

            if self.selected_input is not None:
                self._update_auto_output_path()
                initial_file = Path(self.output_path_var.get() or self.auto_output_path or "").name
                initial_dir = str(self.selected_input.parent)

            output_path = filedialog.asksaveasfilename(
                parent=self.root,
                title="Куда сохранить результат",
                filetypes=[("MP4", "*.mp4"), ("Все файлы", "*.*")],
                defaultextension=".mp4",
                initialdir=initial_dir,
                initialfile=initial_file,
            )
            if output_path:
                self.output_path_var.set(output_path)

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

        def _resolve_output_for_ui(self) -> Path | None:
            raw_value = self.output_path_var.get().strip().strip('"')
            if not raw_value:
                return None
            return normalize_output_path(Path(raw_value))

        def _build_compress_args(self) -> argparse.Namespace:
            if self.selected_input is None:
                raise ValueError("Сначала выберите видеофайл.")

            profile = COMPRESSION_PROFILE_BY_TITLE[self.profile_var.get()]
            return argparse.Namespace(
                input=self.selected_input,
                output=self._resolve_output_for_ui(),
                crf=profile.crf,
                preset=profile.preset,
                width=profile.width,
                audio_bitrate=profile.audio_bitrate,
                no_audio=profile.no_audio,
            )

        def _build_trim_args(self) -> argparse.Namespace:
            if self.selected_input is None:
                raise ValueError("Сначала выберите видеофайл.")

            self._on_trim_entry_changed()
            start = self._parse_non_negative_float(self.trim_start_var.get(), "Начало")
            end = self._parse_non_negative_float(self.trim_end_var.get(), "Конец")

            return argparse.Namespace(
                input=self.selected_input,
                output=self._resolve_output_for_ui(),
                start=start,
                end=end,
            )

        def start_operation(self) -> None:
            if self.worker_thread and self.worker_thread.is_alive():
                return

            try:
                if self._active_mode() == "compress":
                    args = self._build_compress_args()
                    handler = process_compress
                    start_message = "Запущено сжатие..."
                else:
                    args = self._build_trim_args()
                    handler = process_trim
                    start_message = "Запущена обрезка..."
            except ValueError as exc:
                messagebox.showerror("Ошибка", str(exc), parent=self.root)
                self.status_var.set(str(exc))
                return

            self.clear_log()
            self.append_log(start_message)
            if args.output is not None:
                self.append_log(f"Файл результата: {args.output}")
            self._set_busy(True, start_message)

            self.worker_thread = threading.Thread(
                target=self._run_worker,
                args=(handler, args),
                daemon=True,
            )
            self.worker_thread.start()

        def _run_worker(self, handler: object, args: argparse.Namespace) -> None:
            try:
                result = handler(args, quiet=True)
            except OperationError as exc:
                self.root.after(0, lambda: self._finish_with_error(exc))
                return
            except Exception as exc:
                wrapped_error = OperationError(f"Неожиданная ошибка: {exc}")
                self.root.after(0, lambda: self._finish_with_error(wrapped_error))
                return

            self.root.after(0, lambda: self._finish_with_success(result))

        def _finish_with_success(self, result: OperationResult) -> None:
            self._set_busy(False, f"{result.action_name} завершено.")
            self.last_output_path = result.output_path
            self.open_folder_button.configure(state="normal")
            self.append_log("")
            for line in result.details:
                self.append_log(line)

        def _finish_with_error(self, error: OperationError) -> None:
            self._set_busy(False, "Операция завершилась с ошибкой.")
            self.append_log("")
            self.append_log(str(error))
            messagebox.showerror("Ошибка", str(error), parent=self.root)

        def _set_busy(self, busy: bool, status_text: str) -> None:
            self.is_busy = busy
            self.status_var.set(status_text)
            if busy:
                self.progress.start(12)
            else:
                self.progress.stop()

            for widget in self.interactive_widgets:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="disabled" if busy else "readonly")
                else:
                    widget.configure(state="disabled" if busy else "normal")

            if busy:
                self.open_folder_button.configure(state="disabled")
            elif self.last_output_path is not None and self.last_output_path.exists():
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

            output_dir = self.last_output_path.parent
            if os.name == "nt":
                os.startfile(output_dir)
                return

            if sys.platform == "darwin":
                subprocess.Popen(["open", str(output_dir)])
                return

            subprocess.Popen(["xdg-open", str(output_dir)])


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

    known_commands = {"gui", "menu", "compress", "trim", "-h", "--help"}
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

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
