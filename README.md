## Quick start

- Start Ollama (local model runtime):
  - `ollama serve`
  - Wait until `Listening on 127.0.0.1:11434` appears in logs.
- Activate Python venv and run backend server:
  - Windows PowerShell:
    - `& ./venv/Scripts/Activate.ps1`
    - `python .\backend\server.py`

## Troubleshooting

- If the server reports connection refused to Ollama, ensure `ollama serve` is running and reachable at http://localhost:11434.
- You can override the Ollama host using the `OLLAMA_HOST` environment variable in `.env`.
