-- Only display-bearing Typst commands used by the thesis are adapted here.
-- Preserve other raw formats visibly so a new authoring construct stays reviewable.
local function raw(block)
  local s = block.text:match('^%s*(.-)%s*$')
  if block.format == 'html' then
    if s:match('^<!%-%-mgt%-review%-') then return block end
    if s:match('^<!%-%-') and s:match('%-%->$') then return {} end
    return pandoc.read(s, 'html+tex_math_dollars').blocks
  end
  if block.format == 'typst' then
    if s == '#ldu-bibliography()' then return pandoc.Div({}, pandoc.Attr('refs')) end
    if s:match('^#v%([%d%.]+pt%)$') or
       s:match('^#ldu%-toc%(%)%s*#ldu%-body%-start%(%)$') then return {} end
    local body = s:match('^#par%b()%s*%[(.*)%]$')
    if body then
      body = body:gsub('#h%([%d%.]+pt%)', '')
      body = body:gsub('#ldu%-keyword%-label%[([^%]]+)%]', '**%1**')
      body = body:gsub('#text%(%s*font:%s*"Times New Roman",%s*size:%s*12pt,%s*weight:%s*"bold"%s*%)%[Keywords:%]', '**Keywords:**')
      if not body:find('#') then return pandoc.read(body, 'markdown').blocks end
    end
  end
  return pandoc.CodeBlock(block.text, pandoc.Attr('', {'review-source'}))
end

function RawBlock(block) return raw(block) end
function RawInline(part)
  if part.format == 'html' then return part end
  return pandoc.Code(part.text)
end

function CodeBlock(block)
  if block.classes:includes('lesson-table-html') then
    return pandoc.read(block.text, 'html+tex_math_dollars').blocks
  end
end
