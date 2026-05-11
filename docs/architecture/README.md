# Architecture Documentation

This folder contains architecture documentation for the SharePoint ingestion solution, including both **dev** and **prod** environments.

## Artifacts

- `sharepoint_ingestion_architecture.drawio`  
  Editable Visio-style architecture source (diagrams.net / draw.io format)
- `sharepoint_ingestion_architecture.md`  
  Markdown-rendered architecture view (Mermaid) and Azure artifact legend
- `sharepoint_ingestion_sequence.md`  
  Sequence diagrams for pre-checks and ingestion runtime, with connection/port callouts

## How to use Azure image artifacts in Draw.io

1. Open `sharepoint_ingestion_architecture.drawio` in diagrams.net/draw.io.
2. In the left panel, choose **More Shapes...**.
3. Enable Azure shape libraries (Azure / Azure Architecture).
4. Replace placeholder component boxes with official Azure icons (same labels and relationships are already present).

## Network call summary

- Python -> Azure Key Vault: **HTTPS 443**
- Python -> SharePoint Online: **HTTPS 443**
- Python -> SQL Server: **TDS over TCP 1433**
