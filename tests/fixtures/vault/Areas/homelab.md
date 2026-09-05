---
title: homelab
date: 2026-01-11
updated: @@D40@@
tags: [homelab, k3s, backup]
status: active
kind: system
summary: K3s-klustret, Longhorn, Traefik och backuperna.
path: ~/homelab, ~/homelab-gitops
---

Klustret kör Longhorn för lagring och Traefik för ingress. Backuperna går till Minio.
Runners för ci kör på klustret och sparar GitHub-minuter.
