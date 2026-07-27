"""Locations and categories library for the Telegram Spy Game."""

import random

LOCATIONS = [
    {"name": "🏥 مستشفى", "word": "مستشفى"},
    {"name": "✈️ مطار", "word": "مطار"},
    {"name": "🏫 مدرسة", "word": "مدرسة"},
    {"name": "🍕 مطعم", "word": "مطعم"},
    {"name": "🎬 سينما", "word": "سينما"},
    {"name": "⚽ ملعب كرة قدم", "word": "ملعب"},
    {"name": "🏖️ شاطئ البحر", "word": "شاطئ"},
    {"name": "🏛️ متحف", "word": "متحف"},
    {"name": "🥐 مخبز", "word": "مخبز"},
    {"name": "🛸 مركبة فضائية", "word": "فضاء"},
    {"name": "🚢 سفينة ركاب", "word": "سفينة"},
    {"name": "🏨 فندق", "word": "فندق"},
    {"name": "🛒 سوبرماركت", "word": "سوبرماركت"},
    {"name": "🏋️ نادي رياضي (جيم)", "word": "جيم"},
    {"name": "🎪 سيرك", "word": "سيرك"},
    {"name": "🚂 قطار", "word": "قطار"},
    {"name": "🚒 محطة إطفاء", "word": "إطفاء"},
    {"name": "🚓 مركز شرطة", "word": "شرطة"},
    {"name": "🏰 قلعة تاريخية", "word": "قلعة"},
    {"name": "🐪 صحراء", "word": "صحراء"},
    {"name": "🎡 مدينة ألعاب (ملاهي)", "word": "ملاهي"},
    {"name": "⛽ محطة وقود (كازية)", "word": "كازية"},
    {"name": "☕ مقهى (كافيه)", "word": "كافيه"},
    {"name": "💈 صالون حلاقة", "word": "حلاق"},
    {"name": "💊 صيدلية", "word": "صيدلية"},
    {"name": "📚 مكتبة عامة", "word": "مكتبة"},
    {"name": "🏊 مسبح", "word": "مسبح"},
    {"name": "✈️ داخل طائرة", "word": "طائرة"},
    {"name": "🌌 محطة فضاء", "word": "محطة فضاء"},
    {"name": "🛢️ حقل نفط", "word": "حقل نفط"},
    {"name": "🎿 منتجع تزلج", "word": "تزلج"},
    {"name": "🌋 بركان", "word": "بركان"},
    {"name": "🏛️ محكمة", "word": "محكمة"},
    {"name": "🏦 بنك", "word": "بنك"},
    {"name": "🎭 مسرح", "word": "مسرح"},
    {"name": "🚑 سيارة إسعاف", "word": "إسعاف"},
    {"name": "🚀 قاعدة إطلاق صواريخ", "word": "قاعدة صواريخ"},
    {"name": "🛥️ يخت", "word": "يخت"},
    {"name": "🚡 تلفريك", "word": "تلفريك"},
    {"name": "🏬 مجمع تجاري (مول)", "word": "مول"},
    {"name": "🏭 مصنع", "word": "مصنع"},
    {"name": "🌾 مزرعة", "word": "مزرعة"},
    {"name": "🏕️ مخيم صيفي", "word": "مخيم"},
    {"name": "🌋 كهف", "word": "كهف"},
    {"name": "🏔️ قمة جبل", "word": "جبل"},
    {"name": "🏎️ حلبة سباق", "word": "حلبة سباق"},
    {"name": "🚇 مترو الأنفاق", "word": "مترو"},
    {"name": "🏜️ واحة", "word": "واحة"},
    {"name": "⛵ ميناء سفن", "word": "ميناء"},
    {"name": "🏰 قصر ملوكي", "word": "قصر"},
]


def get_random_location() -> dict[str, str]:
    """Return a random secret location dict."""
    return random.choice(LOCATIONS)


def get_location_options(secret_word: str, count: int = 4) -> list[str]:
    """Return a list of location options including the secret_word for Spy guessing."""
    all_words = [loc["word"] for loc in LOCATIONS]
    other_words = [w for w in all_words if w != secret_word]
    random.shuffle(other_words)
    selected = [secret_word] + other_words[: count - 1]
    random.shuffle(selected)
    return selected
