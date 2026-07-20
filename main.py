"""Telegram collage bot for Python 3.14+.

Set TELEGRAM_BOT_TOKEN before running this file.  The bot silently collects
photos, then shows one collage-size message when the user finishes uploading.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# Read the token from your computer's environment.  Do not paste a token here.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8912997548:AAFHvX_fE1849FGn0CkBAGSjY72u18ZmQ_w").strip()
BACKGROUND = (255, 255, 255)
SEND_AS_DOCUMENT = True  # Documents preserve the collage's original quality.
LANCZOS = Image.Resampling.LANCZOS


# Each option is the final canvas size and grid layout.
LAYOUT_SETTINGS = {
    6: {
        "base_w": 1536, "base_h": 2048, "margin_px": 7, "gap_px": 16,
        "inset_px": 4, "cols": 3, "rows": 2,
    },
    8: {
        "base_w": 2048, "base_h": 2048, "margin_px": 10, "gap_px": 10,
        "inset_px": 4, "cols": 4, "rows": 2,
    },
    10: {
        # Five portrait tiles across need a wider canvas.  This keeps every
        # 10-photo tile close to a 1:2 portrait ratio instead of making it
        # narrow and heavily cropped.
        "base_w": 2560, "base_h": 2048, "margin_px": 8, "gap_px": 8,
        "inset_px": 4, "cols": 5, "rows": 2,
    },
    12: {
        "base_w": 1365, "base_h": 2048, "margin_px": 1, "gap_px": 3,
        "inset_px": 4, "cols": 4, "rows": 3,
    },
    18: {
        "base_w": 2048, "base_h": 2048, "margin_px": 5, "gap_px": 5,
        "inset_px": 4, "cols": 6, "rows": 3,
    },
}


user_images: dict[int, list[str]] = {}
user_dirs: dict[int, str] = {}
user_locks: dict[int, asyncio.Lock] = {}
user_upload_finished: set[int] = set()


def upload_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Cancel", callback_data="flow:cancel")],
    ])


def collage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("6 photos", callback_data="collage:6"),
            InlineKeyboardButton("8 photos", callback_data="collage:8"),
            InlineKeyboardButton("10 photos", callback_data="collage:10"),
        ],
        [
            InlineKeyboardButton("12 photos", callback_data="collage:12"),
            InlineKeyboardButton("18 photos", callback_data="collage:18"),
        ],
        [InlineKeyboardButton("Cancel", callback_data="flow:cancel")],
    ])


def ensure_user(user_id: int) -> None:
    if user_id not in user_images:
        user_images[user_id] = []
    if user_id not in user_dirs:
        user_dirs[user_id] = tempfile.mkdtemp(prefix=f"telegram_collage_{user_id}_")
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()


def cleanup_user(user_id: int) -> None:
    directory = user_dirs.pop(user_id, None)
    if directory:
        shutil.rmtree(directory, ignore_errors=True)
    user_images.pop(user_id, None)
    user_upload_finished.discard(user_id)


def split_sizes(total: int, parts: int) -> list[int]:
    base, remainder = divmod(total, parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def cover_to_cell(image: Image.Image, width: int, height: int) -> Image.Image:
    return ImageOps.fit(
        image.convert("RGB"),
        (width, height),
        method=LANCZOS,
        centering=(0.5, 0.5),
    )


def enhance_image(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Brightness(image).enhance(1.10)
    image = ImageEnhance.Contrast(image).enhance(1.06)
    image = ImageEnhance.Color(image).enhance(1.03)
    return ImageEnhance.Sharpness(image).enhance(1.08)


def create_collage(paths: list[str], output_path: str, photo_count: int) -> None:
    settings = LAYOUT_SETTINGS[photo_count]
    cols, rows = settings["cols"], settings["rows"]
    width, height = settings["base_w"], settings["base_h"]
    margin, gap, inset = settings["margin_px"], settings["gap_px"], settings["inset_px"]

    usable_width = width - (2 * margin) - ((cols - 1) * gap)
    usable_height = height - (2 * margin) - ((rows - 1) * gap)
    column_widths = split_sizes(usable_width, cols)
    row_heights = split_sizes(usable_height, rows)
    canvas = Image.new("RGB", (width, height), BACKGROUND)

    image_index = 0
    y = margin
    for row in range(rows):
        x = margin
        for column in range(cols):
            try:
                with Image.open(paths[image_index]) as source:
                    source = source.convert("RGB")
                    source.thumbnail((3000, 3000), LANCZOS)
                    inner_width = max(1, column_widths[column] - (2 * inset))
                    inner_height = max(1, row_heights[row] - (2 * inset))
                    tile = cover_to_cell(source, inner_width, inner_height)
                    tile = enhance_image(tile)
            except (OSError, ValueError) as error:
                raise ValueError(f"Could not read image {image_index + 1}: {error}") from error

            framed_tile = Image.new("RGB", (column_widths[column], row_heights[row]), BACKGROUND)
            framed_tile.paste(tile, (inset, inset))
            canvas.paste(framed_tile, (x, y))
            x += column_widths[column] + gap
            image_index += 1
        y += row_heights[row] + gap

    canvas = canvas.filter(ImageFilter.UnsharpMask(radius=1.2, percent=135, threshold=2))
    canvas.save(output_path, "JPEG", quality=100, subsampling=0, optimize=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    cleanup_user(user_id)
    ensure_user(user_id)
    await update.message.reply_text(
        "Send all of your photos. I will collect them silently.\n\n"
        "When you are finished, send /done. Then choose the collage size.",
        reply_markup=upload_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    cleanup_user(update.effective_user.id)
    await update.message.reply_text("Cancelled. Send /start when you are ready to make a collage.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store a photo without sending a reply for every image."""
    if not update.message or not update.message.photo or not update.effective_user:
        return

    user_id = update.effective_user.id
    ensure_user(user_id)
    lock = user_locks[user_id]
    async with lock:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        index = len(user_images[user_id]) + 1
        path = os.path.join(user_dirs[user_id], f"{index:03d}.jpg")
        await file.download_to_drive(path)
        user_images[user_id].append(path)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Finish the upload phase when the user sends /done."""
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    ensure_user(user_id)
    # Wait for a photo download already in progress before counting files.
    async with user_locks[user_id]:
        if user_id in user_upload_finished:
            return
        image_count = len(user_images[user_id])
        if image_count:
            user_upload_finished.add(user_id)

    if image_count == 0:
        await update.message.reply_text("Please send at least one photo first.")
        return

    await update.message.reply_text(
        f"All files received: {image_count} photo(s).\nChoose the collage size:",
        reply_markup=collage_keyboard(),
    )


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    ensure_user(user_id)
    try:
        requested_count = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Invalid collage size.", show_alert=True)
        return

    images = user_images[user_id]
    if len(images) < requested_count:
        await query.answer(
            f"You need {requested_count - len(images)} more photo(s) for this size.",
            show_alert=True,
        )
        return

    await query.answer()
    await query.edit_message_text(f"Creating your {requested_count}-photo collage...")
    output_path = os.path.join(user_dirs[user_id], f"collage_{requested_count}.jpg")

    try:
        # Rendering is CPU work, so keep Telegram's event loop responsive.
        await asyncio.to_thread(create_collage, images[:requested_count], output_path, requested_count)
        with open(output_path, "rb") as collage_file:
            if SEND_AS_DOCUMENT:
                await query.message.reply_document(
                    collage_file,
                    filename=os.path.basename(output_path),
                    caption="Your collage is ready.",
                )
            else:
                await query.message.reply_photo(collage_file, caption="Your collage is ready.")
    except Exception as error:
        await query.message.reply_text(f"Could not create the collage: {error}")
        return
    finally:
        cleanup_user(user_id)


async def handle_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    cleanup_user(query.from_user.id)
    await query.answer("Cancelled")
    await query.edit_message_text("Cancelled. Send /start when you are ready to make a collage.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Set the TELEGRAM_BOT_TOKEN environment variable before starting the bot.")

    while True:
        # Python 3.14 no longer creates an asyncio event loop automatically.
        # python-telegram-bot's run_polling() needs one to already be current.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        app = (
            ApplicationBuilder()
            .token(TOKEN)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(60)
            .pool_timeout(30)
            .get_updates_connect_timeout(30)
            .get_updates_read_timeout(60)
            .build()
        )
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("done", done))
        app.add_handler(CommandHandler("cancel", cancel))
        app.add_handler(CallbackQueryHandler(handle_cancel_button, pattern=r"^flow:cancel$"))
        app.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^collage:(6|8|10|12|18)$"))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

        try:
            print("Bot running...")
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                bootstrap_retries=5,
                close_loop=True,
            )
            break
        except (TimedOut, NetworkError) as error:
            print(f"Telegram network error: {error}. Retrying in 10 seconds...")
            time.sleep(10)
        finally:
            # A retry must start with a fresh loop. run_polling normally closes
            # it, but this also covers failures before it reaches its cleanup.
            if not loop.is_closed():
                loop.close()
            asyncio.set_event_loop(None)


if __name__ == "__main__":
    main()
