/**
 * ComfyUI-RunPod-Remote — Frontend extension
 * Adds a "Remote Run" button group to the ComfyUI menu.
 *
 * Architecture:
 *   - "▶ Remote Run" → preflight check → submit → poll → results
 *   - "🔍" → SSH auto-scan (fallback: manual form)
 *   - "📋" → manifest summary
 *   - "⚙" → settings (API key, endpoint ID)
 *   - "ℹ" → info & credits (Obiriec Labs)
 *
 * New in v1.1:
 *   - SSH auto-scan: connects to running pod and builds manifest automatically
 *   - K-ORBITAL integration: queue missing models/nodes directly from preflight
 */

import { app } from "../../scripts/app.js";
import { $el } from "../../scripts/ui.js";

// ─── Utilities ────────────────────────────────────────────────────────────────

const API = {
    async get(path) {
        const resp = await fetch(path);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        return resp.json();
    },
    async post(path, body) {
        const resp = await fetch(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
        return resp.json();
    },
};

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ─── CSS spinner (injected once) ─────────────────────────────────────────────

function _ensureSpinStyle() {
    if (!document.getElementById("rpr-spin-style")) {
        const s = document.createElement("style");
        s.id = "rpr-spin-style";
        s.textContent = "@keyframes rpr-spin { to { transform: rotate(360deg); } }";
        document.head.appendChild(s);
    }
}

function makeSpinner() {
    _ensureSpinStyle();
    return $el("span", {
        style: {
            display: "inline-block", width: "12px", height: "12px", flexShrink: "0",
            border: "2px solid #333", borderTopColor: "#00d4ff",
            borderRadius: "50%", animation: "rpr-spin 0.8s linear infinite",
        }
    });
}

// ─── Modal system ─────────────────────────────────────────────────────────────

function createModal(title, contentEl, buttons = []) {
    const overlay = $el("div.rpr-overlay", {
        style: {
            position: "fixed", inset: "0", background: "rgba(0,0,0,0.7)",
            display: "flex", alignItems: "center", justifyContent: "center",
            zIndex: "9999", fontFamily: "var(--content-font, sans-serif)",
        },
        onclick: (e) => { if (e.target === overlay) overlay.remove(); }
    }, [
        $el("div.rpr-modal", {
            style: {
                background: "var(--comfy-menu-bg, #1a1a2e)",
                border: "1px solid var(--border-color, #444)",
                borderRadius: "8px",
                padding: "20px 24px",
                minWidth: "460px",
                maxWidth: "640px",
                maxHeight: "80vh",
                overflowY: "auto",
                color: "var(--input-text, #eee)",
                boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
            }
        }, [
            $el("div", {
                style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }
            }, [
                $el("h2", { style: { margin: "0", fontSize: "16px", fontWeight: "600", color: "#fff" } }, [title]),
                $el("button", {
                    textContent: "✕",
                    style: { background: "none", border: "none", color: "#aaa", fontSize: "18px", cursor: "pointer", padding: "0 4px" },
                    onclick: () => overlay.remove()
                })
            ]),
            contentEl,
            buttons.length > 0
                ? $el("div", {
                    style: { display: "flex", gap: "8px", justifyContent: "flex-end", marginTop: "16px", paddingTop: "12px", borderTop: "1px solid #333" }
                }, buttons)
                : null
        ].filter(Boolean))
    ]);
    document.body.appendChild(overlay);
    return overlay;
}

function btn(label, onclick, style = {}) {
    return $el("button", {
        textContent: label,
        style: {
            background: "var(--comfy-input-bg, #333)",
            border: "1px solid var(--border-color, #555)",
            color: "#eee",
            borderRadius: "4px",
            padding: "6px 14px",
            cursor: "pointer",
            fontSize: "13px",
            ...style
        },
        onclick
    });
}

function statusBadge(text, color) {
    return $el("span", {
        textContent: text,
        style: {
            display: "inline-block",
            padding: "2px 8px",
            borderRadius: "4px",
            fontSize: "12px",
            fontWeight: "600",
            background: color + "33",
            color: color,
            border: `1px solid ${color}66`,
        }
    });
}

// ─── Settings modal ───────────────────────────────────────────────────────────

async function showSettingsModal() {
    let cfg = {};
    try { cfg = await API.get("/runpod/config"); } catch (e) { cfg = {}; }

    const keyAlreadySet = cfg.api_key && cfg.api_key.length > 0;
    const maskedDisplay = keyAlreadySet ? cfg.api_key : null;

    const keyStatusEl = keyAlreadySet
        ? $el("div", {
            style: {
                display: "flex", alignItems: "center", gap: "6px",
                background: "#0a1a0a", border: "1px solid #16a34a",
                borderRadius: "4px", padding: "6px 10px", marginBottom: "6px",
                fontSize: "12px", color: "#4ade80"
            }
        }, [`✅ Chiave già configurata — ${maskedDisplay}`])
        : $el("div", {
            style: {
                background: "#1a0a0a", border: "1px solid #7f1d1d",
                borderRadius: "4px", padding: "6px 10px", marginBottom: "6px",
                fontSize: "12px", color: "#f87171"
            }
        }, ["⚠ Nessuna chiave configurata"]);

    const apiKeyInput = $el("input", {
        type: "password",
        value: "",
        placeholder: keyAlreadySet
            ? "Lascia vuoto per mantenere la chiave esistente"
            : "Inserisci RunPod API Key (rpa_...)",
        style: inputStyle(),
        autocomplete: "off"
    });

    const endpointInput = $el("input", {
        type: "text",
        value: cfg.endpoint_id || "",
        placeholder: "es. a007azjm8d8r4k",
        style: inputStyle()
    });

    const statusEl = $el("div", { style: { marginTop: "8px", fontSize: "13px", color: "#aaa" } });

    const content = $el("div", {}, [
        $el("div", { style: { marginBottom: "12px" } }, [
            $el("label", {
                textContent: "API Key",
                style: { display: "block", fontSize: "12px", color: "#aaa", marginBottom: "4px" }
            }),
            keyStatusEl,
            apiKeyInput,
        ]),
        field("Endpoint ID", endpointInput),
        statusEl,
    ]);

    const overlay = createModal("⚙ RunPod Settings", content, [
        btn("Annulla", () => overlay.remove()),
        btn("Salva", async () => {
            const newKey = apiKeyInput.value.trim();
            const payload = { endpoint_id: endpointInput.value.trim() };
            if (newKey) payload.api_key = newKey;
            try {
                await API.post("/runpod/config", payload);
                statusEl.textContent = "✅ Salvato!";
                statusEl.style.color = "#4ade80";
                setTimeout(() => overlay.remove(), 800);
            } catch (e) {
                statusEl.textContent = "❌ Errore: " + e.message;
                statusEl.style.color = "#f87171";
            }
        }, { background: "#2563eb", borderColor: "#3b82f6" })
    ]);
}

function field(label, inputEl) {
    return $el("div", { style: { marginBottom: "12px" } }, [
        $el("label", {
            textContent: label,
            style: { display: "block", fontSize: "12px", color: "#aaa", marginBottom: "4px" }
        }),
        inputEl
    ]);
}

function inputStyle() {
    return {
        width: "100%", background: "var(--comfy-input-bg, #222)", border: "1px solid #444",
        color: "#eee", borderRadius: "4px", padding: "6px 10px", fontSize: "13px",
        boxSizing: "border-box"
    };
}

// ─── Manifest modal ───────────────────────────────────────────────────────────

async function showManifestModal() {
    const loadingEl = $el("div", { textContent: "Caricamento...", style: { color: "#aaa" } });
    const overlay = createModal("📋 RunPod Manifest", loadingEl);

    let manifest;
    try {
        manifest = await API.get("/runpod/manifest");
    } catch (e) {
        loadingEl.textContent = "Errore: " + e.message;
        return;
    }

    if (!manifest.exists) {
        loadingEl.replaceWith($el("div", {}, [
            $el("p", { style: { color: "#f87171" } }, ["⚠ Nessun manifest trovato."]),
            $el("p", { style: { color: "#aaa", fontSize: "13px" } }, [
                "Clicca 🔍 Scan per scansionare i nodi e modelli installati sull'endpoint."
            ])
        ]));
        return;
    }

    const nodesList = $el("ul", { style: { margin: "4px 0", paddingLeft: "16px", maxHeight: "120px", overflowY: "auto" } },
        (manifest.custom_nodes || []).map(n => $el("li", { style: { fontSize: "12px", color: "#bbb" } }, [n]))
    );
    const modelsList = $el("ul", { style: { margin: "4px 0", paddingLeft: "16px", maxHeight: "160px", overflowY: "auto" } },
        (manifest.models || []).map(m => $el("li", { style: { fontSize: "12px", color: "#bbb" } }, [m]))
    );

    loadingEl.replaceWith($el("div", {}, [
        $el("p", { style: { fontSize: "12px", color: "#888", marginTop: "0" } }, [
            `Scansionato: ${manifest.scanned_at}`
        ]),
        $el("div", { style: { marginBottom: "12px" } }, [
            $el("strong", { style: { color: "#eee" } }, [`Custom Nodes (${manifest.custom_nodes_count})`]),
            nodesList
        ]),
        $el("div", {}, [
            $el("strong", { style: { color: "#eee" } }, [`Modelli (${manifest.models_count})`]),
            modelsList
        ]),
    ]));
}

// ─── Scan: SSH-first, manual fallback ────────────────────────────────────────

async function runScan() {
    const spinRow = $el("div", {
        style: { display: "flex", alignItems: "center", gap: "10px", color: "#aaa", fontSize: "13px" }
    }, [makeSpinner(), "Connessione al pod RunPod via SSH..."]);

    const detailEl = $el("div", {
        style: { fontSize: "11px", color: "#444", marginTop: "8px" }
    }, ["Query RunPod API · SSH · scansione /workspace/custom_nodes e /workspace/models"]);

    const overlay = createModal("🔍 Scan RunPod Worker", $el("div", {}, [spinRow, detailEl]));

    try {
        const result = await API.post("/runpod/scan_ssh", {});
        overlay.remove();

        const successContent = $el("div", {}, [
            $el("div", {
                style: {
                    background: "#0a1a0a", border: "1px solid #16a34a",
                    borderRadius: "6px", padding: "12px 14px", marginBottom: "10px"
                }
            }, [
                $el("div", {
                    style: { color: "#4ade80", fontWeight: "600", marginBottom: "6px" }
                }, ["✅ Manifest aggiornato via SSH"]),
                $el("ul", {
                    style: { margin: "0", paddingLeft: "16px", fontSize: "12px",
                             color: "#86efac", lineHeight: "2" }
                }, [
                    $el("li", {}, [`Pod: ${result.pod_id}`]),
                    $el("li", {}, [`Custom nodes: ${result.custom_nodes_count}`]),
                    $el("li", {}, [`Modelli: ${result.models_count}`]),
                    result.disk_available_human && result.disk_available_human !== "N/A"
                        ? $el("li", {}, [
                            "Spazio disponibile: ",
                            $el("span", {
                                style: {
                                    color: result.disk_available_bytes > 5 * 1024 ** 3
                                        ? "#4ade80" : "#fbbf24",
                                    fontWeight: "600"
                                }
                            }, [result.disk_available_human])
                          ])
                        : null,
                ].filter(Boolean))
            ]),
            $el("p", {
                style: { fontSize: "12px", color: "#555", margin: "0" }
            }, ["Il manifest è pronto. Clicca ▶ Remote Run per inviare il workflow."])
        ]);

        const successOverlay = createModal("✅ Scan completato", successContent, [
            btn("OK", () => successOverlay.remove(), { background: "#16a34a", borderColor: "#22c55e" })
        ]);

    } catch (e) {
        overlay.remove();
        const msg = e.message || String(e);
        const isNoPod = msg.includes("Nessun pod") || msg.includes("pod attivo");
        _showManualScanModal(isNoPod ? "nessun pod RunPod attivo" : msg);
    }
}

// ─── Manual manifest fallback ─────────────────────────────────────────────────

async function _showManualScanModal(sshError) {
    let existing = { custom_nodes: [], models: [] };
    try {
        const m = await API.get("/runpod/manifest");
        if (m.exists) { existing.custom_nodes = m.custom_nodes || []; existing.models = m.models || []; }
    } catch {}

    const taStyle = {
        width: "100%", background: "var(--comfy-input-bg, #1a1a1a)",
        border: "1px solid #444", color: "#eee", borderRadius: "4px",
        padding: "8px", fontSize: "12px", fontFamily: "monospace",
        boxSizing: "border-box", resize: "vertical"
    };

    const nodesArea = $el("textarea", {
        rows: 7,
        placeholder: "ComfyUI-KJNodes\nComfyUI-VideoHelperSuite\nWanVideoWrapper\n...",
        style: taStyle,
        value: existing.custom_nodes.join("\n")
    });

    const modelsArea = $el("textarea", {
        rows: 7,
        placeholder: "checkpoints/juggernaut.safetensors\ndiffusion_models/wan_i2v.safetensors\n...",
        style: taStyle,
        value: existing.models.join("\n")
    });

    const statusEl = $el("div", { style: { fontSize: "12px", color: "#aaa", marginTop: "6px" } });

    const content = $el("div", {}, [
        $el("div", {
            style: { fontSize: "11px", background: "#1a1100", border: "1px solid #555",
                     borderRadius: "4px", padding: "7px 10px", marginBottom: "10px", color: "#888" }
        }, [`⚠ Scan SSH non riuscito (${sshError}). Inserisci il manifest manualmente.`]),
        existing.custom_nodes.length > 0
            ? $el("div", {
                style: { fontSize: "11px", color: "#555", marginBottom: "8px",
                         background: "#111", padding: "4px 8px", borderRadius: "4px" }
            }, [`Manifest esistente pre-caricato: ${existing.custom_nodes.length} nodi · ${existing.models.length} modelli`])
            : null,
        field("Custom Nodes installati sul worker (uno per riga)", nodesArea),
        field("Modelli disponibili sul worker (uno per riga)", modelsArea),
        statusEl,
    ].filter(Boolean));

    const overlay = createModal("🔍 Manifest RunPod — Inserimento Manuale", content, [
        btn("Annulla", () => overlay.remove()),
        btn("Salva Manifest", async () => {
            const nodes = nodesArea.value.split("\n").map(s => s.trim()).filter(Boolean);
            const models = modelsArea.value.split("\n").map(s => s.trim()).filter(Boolean);
            try {
                await API.post("/runpod/manifest/import", { custom_nodes: nodes, models });
                statusEl.textContent = `✅ Salvato: ${nodes.length} nodi, ${models.length} modelli.`;
                statusEl.style.color = "#4ade80";
                setTimeout(() => overlay.remove(), 900);
            } catch (e) {
                statusEl.textContent = "❌ Errore: " + e.message;
                statusEl.style.color = "#f87171";
            }
        }, { background: "#16a34a", borderColor: "#22c55e" })
    ]);
}

// ─── Preflight modal ──────────────────────────────────────────────────────────

async function showPreflightModal(workflow) {
    const statusEl = $el("div", { style: { color: "#aaa", fontSize: "13px" } }, ["Preflight in corso..."]);
    const overlay = createModal("🛫 Preflight Check", statusEl);

    let result;
    try {
        result = await API.post("/runpod/preflight", { workflow });
    } catch (e) {
        statusEl.textContent = "❌ Errore preflight: " + e.message;
        statusEl.style.color = "#f87171";
        return { proceed: false };
    }

    const rows = [];

    if (result.manifest_missing) {
        rows.push($el("div", { style: { background: "#451a03", border: "1px solid #f97316", borderRadius: "4px", padding: "10px", marginBottom: "8px" } }, [
            $el("strong", { style: { color: "#fb923c" } }, ["⚠ Manifest mancante"]),
            $el("p", { style: { margin: "4px 0 0", fontSize: "12px", color: "#fca5a5" } }, [
                "Clicca '🔍 Scan' per scansionare il worker prima di continuare."
            ])
        ]));
    }

    if (result.missing_nodes && result.missing_nodes.length > 0) {
        rows.push(warningBlock("❌ Custom nodes mancanti sul RunPod endpoint", result.missing_nodes, "#f87171", "#7f1d1d"));
    }

    if (result.missing_models && result.missing_models.length > 0) {
        rows.push(warningBlock("❌ Modelli mancanti sul RunPod endpoint", result.missing_models, "#f87171", "#7f1d1d"));
    }

    if (result.unknown_nodes && result.unknown_nodes.length > 0) {
        rows.push(warningBlock("⚠ Nodi non verificabili (non nel mapping)", result.unknown_nodes, "#fbbf24", "#451a03"));
    }

    if (result.warnings && result.warnings.length > 0) {
        result.warnings.forEach(w => rows.push(
            $el("div", { style: { background: "#1a1a1a", border: "1px solid #555", borderRadius: "4px", padding: "8px 10px", marginBottom: "6px", fontSize: "12px", color: "#aaa" } }, [w])
        ));
    }

    // ── K-ORBITAL section (when there are missing files) ─────────────────────
    const hasMissing = !result.manifest_missing &&
        ((result.missing_nodes?.length > 0) || (result.missing_models?.length > 0));

    if (hasMissing) {
        const missingCount = (result.missing_nodes?.length || 0) + (result.missing_models?.length || 0);

        const aria2StatusEl = $el("div", {
            style: { display: "flex", alignItems: "center", gap: "8px", fontSize: "11px", color: "#444" }
        }, [makeSpinner(), "Verifica K-ORBITAL..."]);

        const aria2Box = $el("div", {
            style: {
                background: "#0d1015", border: "1px solid #F5820A1a",
                borderRadius: "6px", padding: "10px 12px", marginBottom: "8px",
            }
        }, [
            $el("div", {
                style: { fontSize: "12px", fontWeight: "700", color: "#F5820A", marginBottom: "4px",
                         letterSpacing: "0.3px" }
            }, ["▲ K-ORBITAL — Carica file mancanti su RunPod"]),
            $el("div", {
                style: { fontSize: "11px", color: "#555", marginBottom: "8px" }
            }, [
                `${missingCount} ${missingCount === 1 ? "file mancante rilevato" : "file mancanti rilevati"}. `,
                "Se hai ",
                $el("span", { style: { color: "#F5820A", fontWeight: "600" } }, ["K-ORBITAL"]),
                $el("sup", {
                    style: { color: "#F5820A88", fontSize: "9px", cursor: "pointer",
                             userSelect: "none", marginLeft: "1px" },
                    title: "K-ORBITAL è un tool separato — vedi nota",
                    onclick: () => _showKOrbitalInfo(),
                }, ["*"]),
                " aperto, puoi avviarli automaticamente.",
            ]),
            aria2StatusEl,
        ]);

        // Footnote
        aria2Box.appendChild($el("div", {
            style: {
                marginTop: "10px", paddingTop: "8px",
                borderTop: "1px solid #F5820A0f",
                fontSize: "10px", color: "#444", lineHeight: "1.5"
            }
        }, [
            $el("sup", { style: { color: "#F5820A66" } }, ["*"]),
            " K-ORBITAL è un tool separato, disponibile a pagamento. ",
            $el("span", {
                style: { color: "#F5820A66", cursor: "pointer", textDecoration: "underline",
                         textDecorationColor: "#F5820A33" },
                onclick: () => _showKOrbitalInfo(),
            }, ["Scopri funzionalità e piani →"]),
        ]));

        rows.push(aria2Box);

        // Async: check K-ORBITAL + fetch manifest disk info + inject action
        (async () => {
            try {
                const [aria2, manifest] = await Promise.all([
                    API.get("/runpod/aria2_check"),
                    API.get("/runpod/manifest").catch(() => null),
                ]);
                aria2StatusEl.replaceWith(_buildAria2Action(aria2, missingCount, result, manifest));
            } catch {
                aria2StatusEl.replaceWith(
                    $el("div", { style: { fontSize: "11px", color: "#333" } },
                        ["Impossibile verificare K-ORBITAL."])
                );
            }
        })();
    }
    // ─────────────────────────────────────────────────────────────────────────

    if (result.ok || result.manifest_missing) {
        const reqs = result.requirements || {};
        rows.push($el("div", { style: { background: "#0a1a0a", border: "1px solid #16a34a", borderRadius: "4px", padding: "10px", marginBottom: "8px" } }, [
            $el("strong", { style: { color: "#4ade80" } }, [result.ok ? "✅ Workflow verificato" : "ℹ Requisiti rilevati"]),
            $el("ul", { style: { margin: "6px 0 0", paddingLeft: "14px", fontSize: "12px", color: "#86efac" } }, [
                $el("li", {}, [`Nodi ComfyUI: ${(reqs.class_types || []).length}`]),
                $el("li", {}, [`Modelli: ${(reqs.models || []).length}`]),
                $el("li", {}, [`Custom nodes necessari: ${(reqs.required_packages || []).join(", ") || "nessuno"}`]),
            ])
        ]));
    }

    const content = $el("div", {}, rows);
    const modal = overlay.querySelector(".rpr-modal");
    modal.replaceChild(content, modal.querySelector("div:nth-child(2)"));

    const canProceed = result.ok;

    return new Promise(resolve => {
        const btnBar = $el("div", {
            style: { display: "flex", gap: "8px", justifyContent: "flex-end", marginTop: "16px", paddingTop: "12px", borderTop: "1px solid #333" }
        }, [
            btn("Annulla", () => { overlay.remove(); resolve({ proceed: false }); }),
            result.manifest_missing
                ? btn("Scan RunPod prima", async () => {
                    overlay.remove();
                    await runScan();
                    resolve({ proceed: false });
                }, { background: "#7c3aed", borderColor: "#8b5cf6" })
                : null,
            btn(
                canProceed ? "▶ Invia a RunPod" : "⚠ Invia comunque",
                () => { overlay.remove(); resolve({ proceed: true }); },
                canProceed
                    ? { background: "#16a34a", borderColor: "#22c55e" }
                    : { background: "#92400e", borderColor: "#f97316" }
            )
        ].filter(Boolean));
        modal.appendChild(btnBar);
    });
}

function _buildAria2Action(aria2, missingCount, preflightResult, manifest) {
    // ── Tool not running ────────────────────────────────────────────────────
    if (!aria2.available) {
        return $el("div", {}, [
            $el("div", { style: { fontSize: "11px", color: "#555", marginBottom: "6px" } }, [
                "K-ORBITAL non in esecuzione sul tuo computer."
            ]),
            $el("div", {
                style: {
                    background: "#0d1015", border: "1px solid #F5820A22",
                    borderRadius: "4px", padding: "7px 10px",
                    fontSize: "11px", color: "#888", lineHeight: "1.6"
                }
            }, [
                $el("span", { style: { color: "#F5820A", fontWeight: "700" } },
                    ["K-ORBITAL"]),
                " è il tool locale Obiriec per gestire tutte le operazioni satellite di ComfyUI: ",
                "download da HuggingFace, CivitAI e URL diretti, installazione custom nodes da GitHub, monitoraggio worker RunPod.",
                $el("br"),
                $el("a", {
                    href: "https://obirieclabs.com",
                    target: "_blank",
                    style: { color: "#F5820A88", textDecoration: "none", fontSize: "10px" }
                }, ["→ Disponibile su obirieclabs.com — pricing in arrivo"])
            ])
        ]);
    }

    // ── Pod not running ─────────────────────────────────────────────────────
    const podRunning = aria2.pod_status === "RUNNING";
    if (!podRunning) {
        return $el("div", { style: { fontSize: "11px", color: "#fbbf24" } }, [
            `⚠ K-ORBITAL attivo, ma nessun pod RunPod RUNNING (stato: ${aria2.pod_status || "N/A"}). `,
            "Avvia un pod su runpod.io prima di caricare."
        ]);
    }

    // ── Disk space badge ────────────────────────────────────────────────────
    const diskInfo = manifest?.disk;
    const diskBadge = diskInfo
        ? $el("div", {
            style: {
                display: "inline-flex", alignItems: "center", gap: "5px",
                background: "#111", border: "1px solid #222",
                borderRadius: "4px", padding: "3px 8px",
                fontSize: "10px", color: "#666", marginBottom: "8px"
            }
        }, [
            $el("span", { style: { color: "#555" } }, ["💾 Spazio RunPod:"]),
            $el("span", { style: { color: diskInfo.available_bytes > 5 * 1024 ** 3 ? "#4ade80" : "#fbbf24", fontWeight: "600" } },
                [diskInfo.available_human]),
            diskInfo.available_bytes === 0
                ? $el("span", { style: { color: "#444" } }, ["(esegui Scan per aggiornare)"])
                : null,
        ].filter(Boolean))
        : $el("div", { style: { fontSize: "10px", color: "#444", marginBottom: "6px" } },
            ["💾 Spazio disponibile: — (esegui 🔍 Scan per rilevarlo)"]);

    // ── Action area ─────────────────────────────────────────────────────────
    const planEl  = $el("div", { style: { marginTop: "8px" } });
    const resultEl = $el("div", { style: { marginTop: "6px", fontSize: "11px" } });

    const uploadBtn = btn(
        `▲ Carica ${missingCount} file su RunPod`,
        async () => {
            uploadBtn.disabled = true;
            uploadBtn.textContent = "Calcolo dimensioni...";
            planEl.innerHTML = "";

            // Step 1: get upload plan (sizes + disk check)
            let plan;
            try {
                plan = await API.post("/runpod/upload_plan", {
                    missing_models: preflightResult.missing_models || [],
                    missing_nodes: preflightResult.missing_nodes || [],
                });
            } catch (e) {
                uploadBtn.disabled = false;
                uploadBtn.textContent = `▲ Carica ${missingCount} file su RunPod`;
                resultEl.textContent = "❌ Errore calcolo: " + e.message;
                resultEl.style.color = "#f87171";
                return;
            }

            // Step 2: show plan — warn if space is tight
            if (!plan.space_ok && plan.space_warning) {
                planEl.appendChild($el("div", {
                    style: {
                        background: "#1a0a00", border: "1px solid #f97316",
                        borderRadius: "4px", padding: "8px 10px", marginBottom: "8px",
                        fontSize: "11px", color: "#fb923c", lineHeight: "1.6"
                    }
                }, [
                    $el("div", { style: { fontWeight: "600", marginBottom: "3px" } },
                        ["⚠ Spazio RunPod insufficiente"]),
                    plan.space_warning,
                    $el("br"),
                    $el("a", {
                        href: "https://www.runpod.io/console/user/storage",
                        target: "_blank",
                        style: { color: "#f97316", fontSize: "10px" }
                    }, ["→ Espandi Network Volume su runpod.io"])
                ]));

                // Add confirm + cancel buttons
                planEl.appendChild($el("div", { style: { display: "flex", gap: "6px", marginTop: "4px" } }, [
                    btn("Annulla", () => {
                        planEl.innerHTML = "";
                        uploadBtn.disabled = false;
                        uploadBtn.textContent = `▲ Carica ${missingCount} file su RunPod`;
                    }, { fontSize: "11px", padding: "4px 10px" }),
                    btn(`⚠ Carica comunque (${plan.total_human})`, async () => {
                        planEl.innerHTML = "";
                        await _doUpload(uploadBtn, resultEl, preflightResult, missingCount);
                    }, { background: "#92400e", borderColor: "#f97316", fontSize: "11px", padding: "4px 10px" }),
                ]));

            } else {
                // Space OK — show summary and proceed
                planEl.appendChild($el("div", {
                    style: { fontSize: "11px", color: "#666", marginBottom: "6px" }
                }, [
                    `Dimensione stimata: ${plan.total_human}`,
                    diskInfo && plan.disk_available_bytes > 0
                        ? `  · Spazio disponibile: ${plan.disk_available_human}  ✅`
                        : "",
                ]));
                await _doUpload(uploadBtn, resultEl, preflightResult, missingCount);
            }
        },
        {
            background: "#F5820A15", borderColor: "#F5820A44", color: "#F5820A",
            fontSize: "12px", padding: "5px 12px"
        }
    );

    return $el("div", {}, [diskBadge, uploadBtn, planEl, resultEl]);
}

async function _doUpload(uploadBtn, resultEl, preflightResult, missingCount) {
    uploadBtn.textContent = "Accodamento in corso...";
    try {
        const res = await API.post("/runpod/trigger_uploads", {
            missing_models: preflightResult.missing_models || [],
            missing_nodes: preflightResult.missing_nodes || [],
        });
        const q   = res.total_queued || 0;
        const nfl = (res.not_found_locally || []).length;
        const unk = (res.unknown_node_repo || []).length;
        const err = (res.errors || []).length;

        let parts = [];
        if (q > 0)   parts.push(`✅ ${q} file accodati in K-ORBITAL`);
        if (nfl > 0) parts.push(`${nfl} non trovati localmente`);
        if (unk > 0) parts.push(`${unk} nodi senza repo GitHub noto`);
        if (err > 0) parts.push(`${err} errori`);
        const msg = parts.join("  ·  ") || "Nessun file accodato.";

        // Space warning from upload response
        if (res.space_warning) {
            resultEl.appendChild($el("div", {
                style: { color: "#fbbf24", marginBottom: "4px" }
            }, [`⚠ ${res.space_warning}`]));
        }

        resultEl.appendChild($el("div", { style: { color: (err > 0 || (!q && nfl > 0)) ? "#fbbf24" : "#4ade80" } }, [msg]));
        uploadBtn.textContent = q > 0 ? `✅ ${q} accodati` : "⚠ 0 accodati";
        uploadBtn.disabled = true;
    } catch (e) {
        uploadBtn.disabled = false;
        uploadBtn.textContent = `▲ Carica ${missingCount} file su RunPod`;
        resultEl.textContent = "❌ " + e.message;
        resultEl.style.color = "#f87171";
    }
}

function warningBlock(title, items, color, bgColor) {
    return $el("div", {
        style: { background: bgColor + "33", border: `1px solid ${color}66`, borderRadius: "4px", padding: "10px", marginBottom: "8px" }
    }, [
        $el("strong", { style: { color, fontSize: "13px" } }, [title]),
        $el("ul", { style: { margin: "6px 0 0", paddingLeft: "14px", fontSize: "12px", color } },
            items.map(i => $el("li", {}, [i]))
        )
    ]);
}

// ─── Job polling + results modal ──────────────────────────────────────────────

async function pollAndShowResults(job_id) {
    const statusEl = $el("div", { style: { fontSize: "14px", color: "#aaa" } }, [`Job: ${job_id}`]);
    const progressEl = $el("div", { style: { fontSize: "13px", color: "#888", marginTop: "8px" } }, ["Status: IN_QUEUE"]);
    const overlay = createModal("🚀 RunPod — Job in corso", $el("div", {}, [statusEl, progressEl]));

    const modal = overlay.querySelector(".rpr-modal");
    const cancelBtn = btn("Annulla job", async () => {
        try { await API.post(`/runpod/cancel/${job_id}`, {}); } catch {}
        progressEl.textContent = "Job annullato.";
        cancelBtn.disabled = true;
    }, { background: "#7f1d1d", borderColor: "#f87171" });
    modal.appendChild($el("div", { style: { marginTop: "12px", display: "flex", justifyContent: "flex-end" } }, [cancelBtn]));

    const STATE_COLORS = {
        IN_QUEUE: "#888",
        IN_PROGRESS: "#60a5fa",
        COMPLETED: "#4ade80",
        FAILED: "#f87171",
        CANCELLED: "#fbbf24",
        TIMED_OUT: "#fbbf24",
        ERROR: "#f87171",
    };

    let done = false;
    let attempts = 0;
    while (!done && attempts < 300) {
        await sleep(2000);
        attempts++;
        let status;
        try { status = await API.get(`/runpod/status/${job_id}`); } catch { await sleep(2000); continue; }

        const state = status.status || "UNKNOWN";
        const color = STATE_COLORS[state] || "#aaa";
        progressEl.innerHTML = "";
        progressEl.appendChild($el("span", {}, [
            "Status: ",
            statusBadge(state, color),
            $el("span", { style: { color: "#555", fontSize: "11px", marginLeft: "8px" } }, [`(poll #${attempts})`])
        ]));

        if (["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "ERROR"].includes(state)) {
            done = true;
            cancelBtn.disabled = true;

            if (state === "COMPLETED") {
                const output = status.raw?.output || status.output || {};
                showJobResults(overlay, job_id, output);
            } else {
                const errDetail = status.raw?.error || status.error || "";
                modal.querySelector("div:nth-child(2)").appendChild(
                    $el("div", { style: { marginTop: "12px", color: "#f87171", fontSize: "13px" } }, [
                        `Job ${state}${errDetail ? ": " + errDetail : ""}`
                    ])
                );
            }
        }
    }

    if (!done) {
        progressEl.textContent = "⏱ Timeout polling lato UI (il job continua su RunPod).";
    }
}

function showJobResults(overlay, job_id, output) {
    const modal = overlay.querySelector(".rpr-modal");
    modal.querySelector("h2").textContent = "✅ RunPod — Risultati";

    const contentArea = modal.querySelector("div:nth-child(2)");
    contentArea.innerHTML = "";

    const images = output.images || [];
    const videos = output.videos || output.gifs || [];
    const allMedia = [...images, ...videos];

    if (allMedia.length === 0) {
        contentArea.appendChild($el("div", { style: { color: "#aaa", fontSize: "13px" } }, [
            "Job completato. Nessun output media trovato.",
            $el("pre", { style: { fontSize: "11px", color: "#555", marginTop: "8px", whiteSpace: "pre-wrap" } },
                [JSON.stringify(output, null, 2).substring(0, 500)]
            )
        ]));
        return;
    }

    contentArea.appendChild($el("div", { style: { color: "#4ade80", fontSize: "13px", marginBottom: "12px" } }, [
        `✅ ${allMedia.length} output ricevuto/i`
    ]));

    allMedia.forEach((item, idx) => {
        const url = typeof item === "string" ? item : (item.url || item.data || "");
        const isVideo = typeof item === "object" && (item.type === "video" || (item.url || "").match(/\.(mp4|webm|gif)$/i));

        if (!url) return;

        const mediaEl = isVideo
            ? $el("video", {
                src: url, controls: true, autoplay: false,
                style: { maxWidth: "100%", borderRadius: "4px", marginBottom: "8px", display: "block" }
            })
            : $el("img", {
                src: url,
                style: { maxWidth: "100%", borderRadius: "4px", marginBottom: "8px", display: "block", cursor: "pointer" },
                onclick: () => window.open(url, "_blank")
            });

        contentArea.appendChild(mediaEl);

        if (!url.startsWith("data:")) {
            contentArea.appendChild(
                btn(`⬇ Scarica output ${idx + 1}`, () => {
                    const a = document.createElement("a");
                    a.href = url; a.download = `runpod_output_${job_id}_${idx + 1}`;
                    a.click();
                }, { fontSize: "11px", padding: "3px 8px", marginBottom: "6px" })
            );
        }
    });
}

// ─── Info modal ───────────────────────────────────────────────────────────────

function showInfoModal() {
    const S = (style) => style;

    const content = $el("div", {}, [

        $el("div", {
            style: S({
                display: "flex", alignItems: "center", gap: "12px",
                background: "linear-gradient(135deg, #030810 60%, #001a2e)",
                border: "1px solid #00d4ff33",
                borderRadius: "8px", padding: "14px 16px", marginBottom: "16px",
            })
        }, [
            $el("div", {
                style: S({
                    fontFamily: "monospace", fontSize: "22px", fontWeight: "700",
                    color: "#00d4ff", letterSpacing: "2px", lineHeight: "1",
                    textShadow: "0 0 12px #00d4ff66"
                })
            }, ["OL"]),
            $el("div", {}, [
                $el("div", {
                    style: S({ fontSize: "14px", fontWeight: "600", color: "#fff", letterSpacing: "0.5px" })
                }, ["Obiriec Labs"]),
                $el("a", {
                    href: "https://obirieclabs.com",
                    target: "_blank",
                    style: S({ fontSize: "11px", color: "#00d4ff", textDecoration: "none", opacity: "0.8" }),
                    onmouseenter: (e) => e.target.style.opacity = "1",
                    onmouseleave: (e) => e.target.style.opacity = "0.8",
                }, ["obirieclabs.com"]),
            ])
        ]),

        $el("div", { style: S({ marginBottom: "14px" }) }, [
            $el("div", {
                style: S({ fontSize: "15px", fontWeight: "600", color: "#fff", marginBottom: "2px" })
            }, ["ComfyUI-RunPod-Remote"]),
            $el("div", {
                style: S({ fontSize: "11px", color: "#888" })
            }, ["v1.1 · © 2026 Obiriec Labs · Tutti i diritti riservati"]),
        ]),

        $el("hr", { style: S({ border: "none", borderTop: "1px solid #222", margin: "0 0 14px" }) }),

        $el("div", {
            style: S({ fontSize: "13px", fontWeight: "600", color: "#bbb",
                       textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: "8px" })
        }, ["Come si usa"]),

        $el("ol", {
            style: S({ margin: "0 0 14px", paddingLeft: "18px",
                       fontSize: "12px", color: "#ddd", lineHeight: "1.8" })
        }, [
            $el("li", {}, ["Clicca ⚙ Settings → inserisci RunPod API key e Endpoint ID."]),
            $el("li", {}, ["Clicca 🔍 Scan → il sistema si collega via SSH al worker e scansiona nodi e modelli automaticamente."]),
            $el("li", {}, ["Apri un workflow in ComfyUI, poi clicca ▶ Remote Run."]),
            $el("li", {}, ["Il preflight verifica nodi e modelli. Se manca qualcosa, puoi caricarli con K-ORBITAL direttamente dal modal."]),
            $el("li", {}, ["Il job viene inviato in formato API — immagini/video compaiono in UI a completamento."]),
        ]),

        $el("div", {
            style: S({ fontSize: "13px", fontWeight: "600", color: "#bbb",
                       textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: "8px" })
        }, ["Funzionalità"]),

        $el("div", {
            style: S({ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px", marginBottom: "14px" })
        }, [
            ["▶ Remote Run",   "Esegui workflow su endpoint serverless"],
            ["🛫 Preflight",   "Verifica nodi e modelli prima dell'invio"],
            ["🔍 Scan SSH",    "Auto-scan worker via SSH — zero config"],
            ["📋 Manifest",    "Visualizza manifest corrente"],
            ["⚙ Settings",    "API key e Endpoint ID persistenti"],
            ["▲ K-ORBITAL",    "Scarica e carica asset su RunPod (HF · CivitAI · GitHub · URL)"],
        ].map(([label, desc]) =>
            $el("div", {
                style: S({ background: "#111", border: "1px solid #222",
                           borderRadius: "4px", padding: "6px 8px" })
            }, [
                $el("div", { style: S({ fontSize: "12px", color: "#00d4ff", marginBottom: "2px" }) }, [label]),
                $el("div", { style: S({ fontSize: "11px", color: "#999" }) }, [desc]),
            ])
        )),

        $el("hr", { style: S({ border: "none", borderTop: "1px solid #222", margin: "0 0 12px" }) }),

        // K-ORBITAL companion badge
        $el("div", {
            style: S({
                display: "flex", alignItems: "center", gap: "12px",
                background: "linear-gradient(135deg, #0d1015, #131820)",
                border: "1px solid #F5820A22",
                borderRadius: "6px", padding: "9px 12px", marginBottom: "12px",
                cursor: "pointer",
            }),
            onclick: () => _showKOrbitalInfo(),
            onmouseenter: (e) => e.currentTarget.style.borderColor = "#F5820A55",
            onmouseleave: (e) => e.currentTarget.style.borderColor = "#F5820A22",
        }, [
            $el("img", {
                src: "/extensions/ComfyUI-RunPod-Remote/k-orbital-logo.png",
                alt: "K-ORBITAL",
                style: S({
                    height: "32px", width: "auto", display: "block", flexShrink: "0",
                    filter: "drop-shadow(0 0 6px #F5820A44)",
                }),
            }),
            $el("div", { style: S({ flex: "1" }) }, [
                $el("div", { style: S({ fontSize: "9px", color: "#F5820A77", letterSpacing: "1.5px", textTransform: "uppercase" }) },
                    ["Asset Depot & Cloud Engine"]),
            ]),
            $el("div", { style: S({ fontSize: "10px", color: "#444" }) }, ["→"]),
        ]),

        $el("div", {
            style: S({ fontSize: "11px", color: "#777", lineHeight: "1.6" })
        }, [
            "Compatibile con ComfyUI 0.24+ · worker-comfyui (fofr/blib-la) · ",
            $el("a", {
                href: "https://obirieclabs.com",
                target: "_blank",
                style: S({ color: "#F5820A99", textDecoration: "none" })
            }, ["obirieclabs.com"])
        ]),
    ]);

    createModal("ℹ ComfyUI-RunPod-Remote", content);
}

// ─── K-ORBITAL info modal ─────────────────────────────────────────────────────

function _showKOrbitalInfo() {
    const S = s => s;
    const OG  = "#F5820A";
    const OG2 = "#FF9B2E";

    // ── Logo ─────────────────────────────────────────────────────────────────
    const brandingSection = $el("div", {
        style: S({
            background: "linear-gradient(135deg, #0d1015, #151922)",
            border: `1px solid ${OG}33`,
            borderRadius: "10px", padding: "16px", marginBottom: "14px",
            textAlign: "center",
        })
    }, [
        $el("img", {
            src: "/extensions/ComfyUI-RunPod-Remote/k-orbital-logo.png",
            alt: "K-ORBITAL",
            style: S({
                maxWidth: "100%", height: "auto", maxHeight: "160px",
                display: "block", margin: "0 auto 8px",
                filter: `drop-shadow(0 0 12px ${OG}44)`,
            }),
        }),
        $el("div", {
            style: S({ fontSize: "10px", color: "#444" })
        }, ["by Obiriec Labs · obirieclabs.com"]),
    ]);

    // ── 4 pillars ─────────────────────────────────────────────────────────────
    const pillars = [
        { label: "SOURCE", desc: "HuggingFace · CivitAI · URL diretti · GitHub" },
        { label: "GATHER", desc: "Fetch automatico · download batch · sync remoto" },
        { label: "BIND",   desc: "Installa nodi · gestisce modelli · aggiorna worker" },
        { label: "DEPLOY", desc: "Esecuzione cloud · RunPod · monitoraggio job" },
    ];

    const pillarsSection = $el("div", {
        style: S({ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px", marginBottom: "14px" })
    }, pillars.map(p =>
        $el("div", {
            style: S({
                background: "#0d1015", border: `1px solid ${OG}1f`,
                borderRadius: "6px", padding: "7px 10px",
            })
        }, [
            $el("div", {
                style: S({ fontSize: "10px", fontWeight: "700", color: OG,
                           letterSpacing: "1.5px", marginBottom: "2px" })
            }, [p.label]),
            $el("div", {
                style: S({ fontSize: "10px", color: "#666", lineHeight: "1.5" })
            }, [p.desc]),
        ])
    ));

    // ── Description ───────────────────────────────────────────────────────────
    const descEl = $el("p", {
        style: S({ fontSize: "12px", color: "#999", margin: "0 0 14px", lineHeight: "1.7" })
    }, [
        "Gestisce tutte le operazioni satellite del tuo workflow ComfyUI remoto: ",
        "download modelli da HuggingFace, CivitAI e URL diretti, ",
        "installazione custom nodes da GitHub, monitoraggio disco RunPod, sync multi-pod. ",
        "Gira sul tuo Mac come app locale e si integra con ComfyUI-RunPod-Remote.",
    ]);

    // ── Pricing tiers ─────────────────────────────────────────────────────────
    const tiers = [
        {
            name: "Free", price: "€0",
            features: ["Scan SSH manifest", "Preflight check", "Upload singolo file"],
            borderColor: "#333", accentColor: "#777",
        },
        {
            name: "Starter", price: "€X/mese",
            features: ["Upload batch modelli", "Install custom nodes", "Monitor disco", "Auto-update"],
            borderColor: OG, accentColor: OG,
        },
        {
            name: "Pro", price: "€X/mese",
            features: ["Tutto Starter +", "Multi-pod sync", "Scheduler upload", "Priority support", "Beta features"],
            borderColor: OG2, accentColor: "#fff",
        },
    ];

    const tiersSection = $el("div", {
        style: S({ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "8px", marginBottom: "14px" })
    }, tiers.map(t =>
        $el("div", {
            style: S({
                background: "#0d1015", border: `1px solid ${t.borderColor}55`,
                borderRadius: "6px", padding: "10px",
            })
        }, [
            $el("div", {
                style: S({ fontSize: "12px", fontWeight: "700", color: t.accentColor, marginBottom: "2px" })
            }, [t.name]),
            $el("div", {
                style: S({ fontSize: "15px", fontWeight: "700", color: "#fff", marginBottom: "7px" })
            }, [t.price]),
            $el("ul", {
                style: S({ margin: "0", paddingLeft: "13px", fontSize: "10px", color: "#666", lineHeight: "1.9" })
            }, t.features.map(f => $el("li", {}, [f])))
        ])
    ));

    // ── CTA ───────────────────────────────────────────────────────────────────
    const ctaEl = $el("div", {
        style: S({
            background: "#0d1015", border: `1px solid ${OG}1f`,
            borderRadius: "6px", padding: "10px 14px",
            fontSize: "11px", color: "#555", textAlign: "center", lineHeight: "1.7",
        })
    }, [
        "🚧 Landing page e sistema di acquisto in arrivo — ",
        $el("a", {
            href: "https://obirieclabs.com",
            target: "_blank",
            style: S({ color: OG + "88", textDecoration: "none" })
        }, ["obirieclabs.com"]),
        " per aggiornamenti.",
    ]);

    const content = $el("div", {}, [
        brandingSection,
        descEl,
        $el("div", {
            style: S({ fontSize: "11px", fontWeight: "700", color: OG + "99",
                       textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" })
        }, ["Quattro Motori"]),
        pillarsSection,
        $el("div", {
            style: S({ fontSize: "11px", fontWeight: "700", color: "#888",
                       textTransform: "uppercase", letterSpacing: "1px", marginBottom: "8px" })
        }, ["Piani disponibili"]),
        tiersSection,
        ctaEl,
    ]);

    createModal("K-ORBITAL — ComfyUI Asset Depot & Cloud Engine", content);
}

// ─── Main "Remote Run" action ─────────────────────────────────────────────────

async function runOnRunpod() {
    let workflow;
    try {
        const graphData = await app.graphToPrompt();
        workflow = graphData.output;
    } catch (e) {
        alert("❌ Impossibile ottenere il workflow: " + e.message);
        return;
    }

    if (!workflow || Object.keys(workflow).length === 0) {
        alert("❌ Workflow vuoto. Aggiungi nodi prima di inviare.");
        return;
    }

    const preflightResult = await showPreflightModal(workflow);
    if (!preflightResult.proceed) return;

    let submitResult;
    try {
        submitResult = await API.post("/runpod/submit", { workflow });
    } catch (e) {
        alert("❌ Errore invio a RunPod: " + e.message);
        return;
    }

    const { job_id } = submitResult;
    if (!job_id) {
        alert("❌ Nessun job_id nella risposta RunPod.");
        return;
    }

    await pollAndShowResults(job_id);
}

// ─── Extension registration ───────────────────────────────────────────────────

app.registerExtension({
    name: "ComfyUI.RunPodRemote",

    async setup() {
        try {
            const { ComfyButtonGroup } = await import("../../scripts/ui/components/buttonGroup.js");
            const { ComfyButton } = await import("../../scripts/ui/components/button.js");

            const runBtn = new ComfyButton({
                icon: "cloud-upload",
                action: runOnRunpod,
                tooltip: "Esegui workflow su RunPod Serverless",
                content: "Remote Run",
                classList: "comfyui-button comfyui-menu-mobile-collapse primary",
            });

            const scanBtn = new ComfyButton({
                icon: "magnify",
                action: runScan,
                tooltip: "Scan worker via SSH — aggiorna manifest nodi e modelli",
            });

            const manifestBtn = new ComfyButton({
                icon: "clipboard-list",
                action: showManifestModal,
                tooltip: "Mostra manifest RunPod (nodi e modelli installati)",
            });

            const settingsBtn = new ComfyButton({
                icon: "cog",
                action: showSettingsModal,
                tooltip: "Impostazioni RunPod (API key, endpoint)",
            });

            const infoBtn = new ComfyButton({
                content: "ℹ",
                action: showInfoModal,
                tooltip: "Info — ComfyUI-RunPod-Remote by Obiriec Labs",
            });

            const group = new ComfyButtonGroup(
                runBtn.element,
                scanBtn.element,
                manifestBtn.element,
                settingsBtn.element,
                infoBtn.element,
            );

            if (app.menu?.settingsGroup?.element) {
                app.menu.settingsGroup.element.before(group.element);
            }

            console.log("[ComfyUI-RunPod-Remote] Menu buttons registered ✓");
        } catch (err) {
            console.warn("[ComfyUI-RunPod-Remote] New-style menu unavailable, falling back:", err.message);
            _addLegacyButtons();
        }
    },
});

function _addLegacyButtons() {
    const menu = document.querySelector(".comfy-menu");
    if (!menu) {
        setTimeout(_addLegacyButtons, 1000);
        return;
    }

    const container = $el("div", {
        style: { display: "flex", gap: "4px", margin: "4px 0", flexWrap: "wrap" }
    }, [
        btn("▶ Remote Run", runOnRunpod, {
            background: "#16a34a", borderColor: "#22c55e",
            fontSize: "12px", padding: "5px 10px"
        }),
        btn("🔍", runScan, { fontSize: "12px", padding: "5px 8px" }),
        btn("📋", showManifestModal, { fontSize: "12px", padding: "5px 8px" }),
        btn("⚙", showSettingsModal, { fontSize: "12px", padding: "5px 8px" }),
        btn("ℹ", showInfoModal, { fontSize: "12px", padding: "5px 8px" }),
    ]);

    menu.appendChild(container);
    console.log("[ComfyUI-RunPod-Remote] Legacy menu buttons registered ✓");
}
