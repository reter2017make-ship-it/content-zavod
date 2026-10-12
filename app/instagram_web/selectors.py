"""Every place where the module looks at Instagram's page. When Instagram changes its interface, fix it here.

Text is matched in Russian and English (the interface language follows the account). Roles and texts are
preferred to CSS classes, which Instagram regenerates."""
from __future__ import annotations

import re

HOME = "https://www.instagram.com/"
LOGIN = "https://www.instagram.com/accounts/login/"
EDIT_PROFILE = "https://www.instagram.com/accounts/edit/"

# cookie that exists only in a signed-in session (read from the browser's own profile, never saved by us)
SESSION_COOKIE = "sessionid"
USER_ID_COOKIE = "ds_user_id"

# pages where Instagram wants a person: never automated, the queue stops and waits
CHECKPOINT_URL = re.compile(r"/(challenge|checkpoint|two_factor|accounts/suspended|accounts/disabled|"
                            r"auth_platform|accounts/login/two_factor)", re.I)
CHECKPOINT_TEXT = re.compile(
    r"(подтвердите,? что это вы|подозрительн|введите код|security code|confirm it's you|suspicious|"
    r"help us confirm|we suspended|мы приостановили|captcha|проверка безопасности|security check)", re.I)

# username: the profile link in the side navigation, then the field on the «edit profile» page
PROFILE_PICTURE_ALT = re.compile(r"(profile picture|фото профиля)", re.I)
USERNAME_INPUT = 'input[name="username"]'
RESERVED_PATHS = {"explore", "reels", "direct", "accounts", "stories", "p", "reel", "about", "legal", "web",
                  "emails", "challenge", "developer", "api", "graphql", "static", "privacy"}

# ---------- creating a Reel (checked on the live site 2026-10-07, Russian interface) ----------
TEXT_CREATE = ("Создать", "Create")            # side navigation link
# when the side navigation is collapsed to icons there is no text: the icon's label is used
ICON_CREATE = ("Новая публикация", "New post", "Создать", "Create")
TEXT_POST = ("Публикация", "Post")             # item of the «Создать» menu
DIALOG = "[role=dialog]"
FILE_INPUT = "[role=dialog] input[type=file]"  # accept: …video/mp4,video/quicktime
COVER_INPUT = "[role=dialog] input[type=file][accept*='image']"
TEXT_OK = ("OK", "ОК")                          # «Теперь видеопубликациями можно делиться как видео Reels»
TEXT_NEXT = ("Далее", "Next")
TEXT_SHARE = ("Поделиться", "Share")
TEXT_DISCARD = ("Удалить", "Discard")
CAPTION_BOX = "[role=dialog] [role=textbox][contenteditable=true]"
# crop step: Instagram starts at 1:1 — the factory always picks «Оригинал» (its videos are 9:16)
ICON_CROP_SIZE = ("Выбрать размер и обрезать", "Select crop")
TEXT_ORIGINAL = ("Оригинал", "Original")
TEXT_9_16 = ("9:16",)
# width/height of the part of the video visible in the crop preview (≈0.56 for 9:16, 1.0 for a square)
VISIBLE_RATIO_JS = """() => { const d = [...document.querySelectorAll('[role=dialog]')].find(x => x.offsetParent);
  if (!d) return null; const v = [...d.querySelectorAll('video, img')].map(e => e.getBoundingClientRect())
  .filter(r => r.width > 150).sort((a, b) => b.width * b.height - a.width * a.height)[0]; if (!v) return null;
  const b = d.getBoundingClientRect(), top = Math.max(v.top, b.top + 44), bottom = Math.min(v.bottom, b.bottom),
  left = Math.max(v.left, b.left), right = Math.min(v.right, b.right);
  return bottom > top ? (right - left) / (bottom - top) : null; }"""
# step titles (dialog aria-label / heading)
STEP_CROP = re.compile(r"^(обрезать|crop)$", re.I)
STEP_EDIT = re.compile(r"^(редактировать|edit)$", re.I)
STEP_FINAL = re.compile(r"(новое видео reels|new reel|создание публикации|create new post)", re.I)
# after «Поделиться»
SHARE_DONE = re.compile(r"(видео reels опубликовано|публикация опубликована|ваше видео reels|ваша публикация|"
                        r"reel has been shared|your reel has been shared|post shared|has been shared)", re.I)
SHARE_FAILED = re.compile(r"(не удалось (опубликовать|поделиться|загрузить)|не удалось|произошла ошибка|something went wrong|"
                          r"couldn't (share|upload|post)|try again|попробуйте (ещё раз|еще раз|снова))", re.I)
UPLOAD_FAILED = re.compile(r"(файл не поддерживается|this file is not supported|не удалось загрузить|couldn't upload|"
                           r"видео слишком|video is too)", re.I)

# ---------- switching between linked accounts (checked on the live site 2026-10-07) ----------
TEXT_SWITCH = ("Переключиться", "Switch")          # next to the own @name on the home page
SWITCH_DIALOG = re.compile(r"(переключение между аккаунтами|switch accounts)", re.I)
TEXT_ADD_ACCOUNT = ("Войти в существующий аккаунт", "Log into an existing account", "Log in to an existing account")
PASSWORD_INPUT = "input[type=password], input[name=password]"
