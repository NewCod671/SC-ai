"""Small OOXML fixtures; no external template or assumed meaning of SC."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape

def workbook(controls=None):
    controls = controls or [('SC-A', 'Review access permissions quarterly', 'IT'), ('SC-B', 'Train employees on data handling', 'HR')]
    values = [('Control ID','Requirement','Department','Answer','Status','Result','Question'), *[(*c, '', 'unanswered', '', '') for c in controls]]
    rows = []
    for r, vals in enumerate(values, 1):
        cells = ''.join(f'<c r="{chr(65+i)}{r}" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for i,v in enumerate(vals))
        rows.append(f'<row r="{r}" ht="25" customHeight="1">{cells}</row>')
    parts = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Checklist" sheetId="1" r:id="rId1"/><sheet name="Reference" sheetId="2" r:id="rId2"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="7" width="24" customWidth="1"/></cols><sheetData>'+''.join(rows)+'</sheetData><mergeCells count="1"><mergeCell ref="I1:J1"/></mergeCells><pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>',
        'xl/worksheets/sheet2.xml': '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><f>SUM(1,2)</f><v>3</v></c></row></sheetData></worksheet>',
        'customXml/item1.xml': '<original>preserve unknown template parts</original>',
    }
    stream = BytesIO()
    with ZipFile(stream, 'w', ZIP_DEFLATED) as z:
        for name, value in parts.items():
            z.writestr(name, value)
    return stream.getvalue()

def patch_cell(data, row, column, value):
    from app.reporting import excel
    from lxml import etree as ET
    with ZipFile(BytesIO(data)) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    root = ET.fromstring(parts['xl/worksheets/sheet1.xml'])
    excel.set_cell(root, row, column, value)
    parts['xl/worksheets/sheet1.xml'] = ET.tostring(root)
    out = BytesIO()
    with ZipFile(out, 'w', ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return out.getvalue()
