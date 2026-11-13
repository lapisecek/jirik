---
name: slozky
title: Složky – vytvořit, otevřít, uklidit plochu
description: Vytvořit novou složku, otevřít složku, ukázat co je na ploše nebo v Dokumentech, uklidit soubory do složky.
keywords: slozk, adresar, vytvor, novou slozku, otevri slozku, plocha, plose, dokument, uklid, poradek, roztrid
devices: pc
tools: create_folder, list_files, move_files, open_item
---
## Postup pro Aiko
1. New folder: create_folder(name="<name>", where="desktop" or "documents"). If it already existed, say so.
2. Show a folder: open_item(where="desktop/<name>") or open_item(where="documents").
3. Tidy up ("uklid plochu"): list_files(where="desktop"), then move matching files with
   move_files(names_json, source="desktop", target="documents/<folder>") - only files the senior asked about.
   Never move folders the senior did not mention and never delete anything.
4. done: Czech summary of what changed and where to find it.

## Pro seniora
1. Na ploše klikněte pravým tlačítkem myši na prázdné místo, ne na ikonu.
2. V nabídce najeďte na „Nový“ a pak klikněte na „Složka“.
3. Napište název složky a zmáčkněte Enter, velkou klávesu vpravo se zahnutou šipkou.
4. Složku otevřete tak, že na ni dvakrát rychle kliknete levým tlačítkem myši.
