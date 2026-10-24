import { useMemo, useRef, useState, type ReactNode } from 'react';
import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';
import javascript from 'highlight.js/lib/languages/javascript';
import java from 'highlight.js/lib/languages/java';
import json from 'highlight.js/lib/languages/json';
import bash from 'highlight.js/lib/languages/bash';
import 'highlight.js/styles/github.css';

for (const [name, grammar] of Object.entries({ python, javascript, java, json, bash })) hljs.registerLanguage(name, grammar);
export function reference(raw: string): { path: string; line: number } | null {
  const match = /^([^:#?\n]+?)(?::(\d+)(?:-\d+)?)?$/.exec(raw);
  if (!match || !/\.[\w-]+$/.test(match[1]) || match[1].startsWith('/') || match[1].split('/').some(p => p === '..' || p === '.git' || p === '.pico')) return null;
  const line = Number(match[2] || 1);
  return Number.isSafeInteger(line) && line > 0 && match[1].length <= 1024 ? { path: match[1], line } : null;
}
function Code({ className, children, onFile }: { className?: string; children?: ReactNode; onFile: (path: string, line: number) => void }) {
  const [copied, setCopied] = useState(false);
  const text = String(children ?? ''), language = className?.replace('language-', '');
  const target = !className && !text.includes('\n') ? reference(text) : null;
  if (target) return <button className="file-reference" onClick={() => onFile(target.path, target.line)}>{text}</button>;
  if (!language && !text.includes('\n')) return <code>{children}</code>;
  const value = language && hljs.getLanguage(language) && text.length <= 32768 ? hljs.highlight(text, { language }).value : null;
  return <span className="code-block"><span className="code-actions">{language || 'text'}<button type="button" onClick={() => {
    void window.pico.writeClipboard(text).then(() => setCopied(true), () => setCopied(false));
  }}>{copied ? '已复制' : '复制代码'}</button></span>{value ? <code className="hljs" dangerouslySetInnerHTML={{ __html: value }} /> : <code>{text}</code>}</span>;
}
export function Answer({ text, onFile }: { text: string; onFile: (path: string, line: number) => void }) {
  const file = useRef(onFile); file.current = onFile;
  const components = useMemo<Components>(() => ({
    img: ({ alt }) => <span className="muted">[图片：{alt || '未自动加载'}]</span>,
    a: ({ href, children }) => <button className="text-link" onClick={() => {
      const local = href && reference(href);
      if (local) file.current(local.path, local.line);
      else if (href && /^https?:\/\//.test(href)) void window.pico.openExternal(href).catch(() => {});
    }}>{children}</button>,
    code: ({ className, children }) => <Code className={className} onFile={(path, line) => file.current(path, line)}>{children}</Code>,
  }), []);
  return <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={components}>{text}</Markdown></div>;
}
