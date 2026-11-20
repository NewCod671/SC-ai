# Workbook reporting

- `excel.py`: workbook validation, normalized template import/export, metadata,
  preview, and shared OOXML style helpers.
- `native_sc.py`: the bundled original SC workbook adapter, grade markers,
  dashboard/chart caches and native answer import.
- `audit_report.py`: the normalized SC Audit Note worksheet adapter.

This package accepts existing validated controls, answers and findings. Model
inference belongs to the provider/workflow modules. Template files are located
in `backend/templates`, outside this Python package.
