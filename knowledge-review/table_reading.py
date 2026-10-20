"""Read known table rows without confusing top-k retrieval with complete coverage."""

from markdown_it import MarkdownIt


def table_rows(source, block):
    text = '\n'.join(source.text.splitlines()[block.line_start - 1:block.line_end])
    tokens = MarkdownIt('commonmark').enable('table').parse(text)
    rows, headers, in_header = [], [], False
    for index, token in enumerate(tokens):
        if token.type == 'thead_open':
            in_header = True
        elif token.type == 'thead_close':
            in_header = False
        elif token.type == 'tr_open' and token.map:
            cells = []
            for child in tokens[index + 1:]:
                if child.type == 'tr_close':
                    break
                if child.type == 'inline':
                    cells.append(child.content)
            row = {'cells': cells, 'line_start': block.line_start + token.map[0],
                   'line_end': block.line_start + token.map[1] - 1}
            (headers if in_header else rows).append(row)
    if not headers or not rows:
        raise ValueError('表格暂未形成可可靠分页的行结构，请读取原文或查看原图。')
    return headers, rows


def visible_table_coverage(chunks, sources):
    """Recompute coverage after budget trimming, from text actually sent onward."""
    from context import citation_is_visible
    result = []
    for source in sources:
        if source.document is None:
            continue
        visible = [c for c in chunks if c.source_id == source.source_id]
        for block in source.document.blocks:
            if block.kind != 'table' or not any(c.line_start <= block.line_end and c.line_end >= block.line_start for c in visible):
                continue
            try:
                headers, rows = table_rows(source, block)
            except ValueError:
                result.append({'source_id': source.source_id, 'block_id': block.block_id, 'complete_extracted_table': False,
                               'notice': 'Table row structure could not be verified.'})
                continue
            indices = [i for i, row in enumerate(rows, 1)
                       if citation_is_visible(source.source_id, row['line_start'], row['line_end'], visible)]
            ranges = []
            for index in indices:
                if ranges and ranges[-1][1] == index - 1:
                    ranges[-1][1] = index
                else:
                    ranges.append([index, index])
            header_visible = all(citation_is_visible(source.source_id, h['line_start'], h['line_end'], visible) for h in headers)
            result.append({'source_id': source.source_id, 'block_id': block.block_id,
                           'total_extracted_rows': len(rows), 'visible_data_rows': len(indices),
                           'visible_row_ranges': ranges, 'header_visible': header_visible,
                           'complete_extracted_table': header_visible and len(indices) == len(rows)})
    return result
