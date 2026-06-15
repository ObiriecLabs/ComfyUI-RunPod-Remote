# ComfyUI-RunPod-Remote

Esegui i tuoi workflow ComfyUI direttamente su RunPod serverless con un click.

## Features

- **▶ RunPod** — preflight check + invio workflow + polling risultati
- **🔍 Scan** — scansiona i custom nodes e modelli installati sull'endpoint
- **📋 Manifest** — visualizza il manifest dell'endpoint (nodi e modelli)
- **⚙ Settings** — configura API key e endpoint ID

## Preflight Check

Prima di ogni invio, il sistema verifica:
1. **Custom nodes** — tutti i nodi usati nel workflow sono installati sull'endpoint
2. **Modelli** — tutti i checkpoint/LoRA/ControlNet sono presenti sul volume RunPod

Se qualcosa manca, il preflight mostra un avviso e permette di annullare o procedere comunque.

## Setup

1. Clicca **⚙ Settings** e inserisci:
   - **API Key**: la tua RunPod API key (da runpod.io/console)
   - **Endpoint ID**: l'ID del tuo endpoint serverless worker-comfyui

2. Clicca **🔍 Scan** la prima volta per popolare il manifest dell'endpoint
   - Il nodo `RunPodSystemInfo` (incluso in questo package) deve essere installato sul worker
   - In alternativa, importa il manifest manualmente via `/runpod/manifest/import`

3. Clicca **▶ RunPod** per eseguire il workflow corrente su RunPod

## Formato workflow

Il workflow viene inviato in **formato API** (`app.graphToPrompt().output`), che è il formato accettato da worker-comfyui.

## Endpoint compatibile

Testato con: `worker-comfyui` (fofr/comfyui-api su RunPod)

## File locali

- `config.json` — API key e endpoint ID (creato automaticamente)
- `manifest_cache.json` — cache dei nodi/modelli dell'endpoint (aggiornato via Scan)
