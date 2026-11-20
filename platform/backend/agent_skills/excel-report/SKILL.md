---
name: excel-report
description: Preserve control mapping when supplying results to the existing Excel exporter.
---

Return the original control_id and the existing Finding fields unchanged. Do not edit workbook files, formulas, styles, mappings or grades directly. The application validates results and calls its existing Excel exporter for an explicitly labeled experimental export. Experimental results must never overwrite a production/Colab run or its export. Auditor corrections remain a separate record.
