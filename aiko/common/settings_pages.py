"""Stránky nastavení, které smí Aiko otevřít přímo (bez klikání po menu).

Jen otevření – změnu pak agent udělá klikáním a senior ji vidí. Bezpečnostní stránky
(účty, hesla, obnovení, zabezpečení, pro vývojáře) v seznamu záměrně nejsou.
klíč: (Windows ms-settings:, Android intent akce, popis pro seniora)
"""
from __future__ import annotations

SETTINGS_PAGES: dict[str, tuple[str, str, str]] = {
    "dark_mode": ("ms-settings:colors", "android.settings.DISPLAY_SETTINGS", "barvy a tmavý režim"),
    "display": ("ms-settings:display", "android.settings.DISPLAY_SETTINGS", "obrazovka a jas"),
    "text_size": ("ms-settings:easeofaccess-display", "android.settings.DISPLAY_SETTINGS", "velikost textu"),
    "sound": ("ms-settings:sound", "android.settings.SOUND_SETTINGS", "zvuk"),
    "bluetooth": ("ms-settings:bluetooth", "android.settings.BLUETOOTH_SETTINGS", "Bluetooth"),
    "wifi": ("ms-settings:network-wifi", "android.settings.WIFI_SETTINGS", "Wi-Fi"),
    "background": ("ms-settings:personalization-background", "", "pozadí plochy"),
    "mouse": ("ms-settings:mousetouchpad", "", "myš"),
    "printers": ("ms-settings:printers", "", "tiskárny"),
    "date_time": ("ms-settings:dateandtime", "android.settings.DATE_SETTINGS", "datum a čas"),
    "battery": ("ms-settings:batterysaver", "android.settings.BATTERY_SAVER_SETTINGS", "baterie"),
    "notifications": ("ms-settings:notifications", "android.settings.NOTIFICATION_SETTINGS", "oznámení"),
    "language": ("ms-settings:regionlanguage", "android.settings.LOCALE_SETTINGS", "jazyk"),
    "accessibility": ("ms-settings:easeofaccess", "android.settings.ACCESSIBILITY_SETTINGS", "usnadnění"),
}
