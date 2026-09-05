---
title: "Bokning: fällan i avbokningen"
date: 2026-02-01
updated: @@D40@@
tags: [booking, greenhouse]
status: complete
kind: trap
area: "[[greenhouse]]"
summary: En avbokning som inte frigör sin slot lämnar lokalen låst.
---

En avbokning måste frigöra sina bokade slots i samma transaktion, annars står lokalen låst.
Infördes i 9ca31382 och gäller alla bokningsbara kort.
