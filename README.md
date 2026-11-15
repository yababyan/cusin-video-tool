# CUSINI ROYAL VIDEO TOOL

## Как запустить

1. Откройте раздел `Releases`
2. Скачайте файл `CUSINI_ROYAL_VIDEO_TOOL.exe`
3. Запустите его
4. Выберите видео или GIF
5. Отметьте фрагмент ползунками (по умолчанию берётся всё видео)
6. Выберите, что сделать, и нажмите зелёную кнопку

| Режим | Что делает |
|---|---|
| **В Discord** | Обрезает и сжимает за один проход. Битрейт, разрешение и FPS подбираются под длину фрагмента, файл гарантированно влезает в лимит (по умолчанию 15 МБ). |
| **Шакал 2000-х** | Кадр 4:3 640×480, 12 FPS, звук +20% и watermark Bandicam. Тоже укладывается в лимит. |
| **GIF** | GIF из любого видео или GIF: ширина, кроп 1:1 / 4:3 / 16:9, FPS, цвета и необязательный максимальный вес. |
| **Своё** | Пресеты сжатия по качеству (CRF) и ручной размер кадра. |

Результат сохраняется рядом с исходником; другой путь можно выбрать через «Сохранить как…».

## Ссылка

`https://github.com/yababyan/cusin-video-tool/releases`

## Для разработки

Если нужно запустить проект из исходников:

```powershell
python -m pip install -r requirements.txt
python .\compress_video.py
```

Если нужно пересобрать `.exe`:

```powershell
.\build_exe.bat
```

Те же сценарии из консоли:

```powershell
python .\compress_video.py export .\clip.mp4 --start 12 --end 45 --limit 15
python .\compress_video.py export .\clip.mp4 --mode retro
python .\compress_video.py export .\clip.mp4 --mode gif --start 3 --end 6 --gif-aspect 1:1
```
