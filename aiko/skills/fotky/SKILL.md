---
name: fotky
title: Prohlížet fotky a obrázky
description: Ukázat fotky z dovolené, od vnoučat, stažené obrázky; otevřít složku Obrázky, prohlížet fotky jednu po druhé.
keywords: fotk, fotografi, obrazk, obrazek, snimk, vnouc, dovolen, album, galeri, prohlizet, ukaz
devices: pc
tools: list_files, find_files, open_item
first_tool: list_files
---
## Postup pro Aiko
1. Recent photos (e.g. sent by family): list_files(where="downloads", days=7, kind="images"), then
   list_files(where="pictures", days=30, kind="images").
2. A named album/folder: find_files(name="<word>") and open the folder with open_item(where=...).
3. Open the first photo with open_item(name, where) - it opens in the Photos app, where arrow keys
   (key right / key left) show the next / previous photo.
4. done: tell the senior in Czech that the photos are open and that the arrow keys on the keyboard move between them.

## Pro seniora
1. Fotka je otevřená. Další fotku ukáže šipka vpravo na klávesnici, předchozí šipka vlevo.
2. Fotku zvětšíte kolečkem uprostřed myši.
3. Zavřete ji křížkem vpravo nahoře.
