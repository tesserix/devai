/**
 * Minimal YAML dump/parse for registry manifests (Kubernetes-style maps,
 * arrays, and scalars). Self-contained — no dependency — so the authoring
 * editor can render a live YAML view and parse hand edits without pulling a
 * full YAML library into the dashboard bundle.
 *
 * Scope: nested maps, arrays (of scalars or maps), and scalars (string / number
 * / boolean / null). Anything that needs quoting is emitted as a JSON
 * double-quoted string (valid YAML), and parsed back with JSON.parse — which
 * keeps multi-line and special-character values lossless. It is NOT a general
 * YAML implementation (no anchors, tags, or block scalars); the editor's JSON
 * view is always available as the lossless fallback.
 */

type Json = unknown;

const PLAIN_SCALAR = /^[A-Za-z0-9][\w .,/@:+-]*$/;
const NEEDS_QUOTE = (s: string): boolean =>
  s === "" ||
  !PLAIN_SCALAR.test(s) ||
  /^(true|false|null|~|yes|no|on|off)$/i.test(s) ||
  /^[+-]?(\d|\.)/.test(s); // number-looking strings must be quoted to stay strings

function dumpScalar(v: Json): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : JSON.stringify(String(v));
  const s = String(v);
  return NEEDS_QUOTE(s) ? JSON.stringify(s) : s;
}

function isObj(v: Json): v is Record<string, Json> {
  return v != null && typeof v === "object" && !Array.isArray(v);
}

export function dump(value: Json, indent = 0): string {
  const pad = "  ".repeat(indent);

  if (Array.isArray(value)) {
    if (value.length === 0) return `${pad}[]\n`;
    let out = "";
    for (const item of value) {
      if (isObj(item) || Array.isArray(item)) {
        const block = dump(item, indent + 1);
        // Hang the first line of the nested block onto the "- " marker.
        const firstNl = block.indexOf("\n");
        const first = block.slice(0, firstNl).trimStart();
        const rest = block.slice(firstNl + 1);
        out += `${pad}- ${first}\n${rest}`;
      } else {
        out += `${pad}- ${dumpScalar(item)}\n`;
      }
    }
    return out;
  }

  if (isObj(value)) {
    const keys = Object.keys(value);
    if (keys.length === 0) return `${pad}{}\n`;
    let out = "";
    for (const k of keys) {
      const v = value[k];
      if (Array.isArray(v)) {
        out += v.length === 0 ? `${pad}${k}: []\n` : `${pad}${k}:\n${dump(v, indent)}`;
      } else if (isObj(v)) {
        out += Object.keys(v).length === 0 ? `${pad}${k}: {}\n` : `${pad}${k}:\n${dump(v, indent + 1)}`;
      } else {
        out += `${pad}${k}: ${dumpScalar(v)}\n`;
      }
    }
    return out;
  }

  return `${pad}${dumpScalar(value)}\n`;
}

function parseScalar(raw: string): Json {
  const s = raw.trim();
  if (s === "" || s === "~" || s === "null") return s === "" ? "" : null;
  if (s === "true") return true;
  if (s === "false") return false;
  if (s === "{}") return {};
  if (s === "[]") return [];
  if (s[0] === '"') {
    return JSON.parse(s);
  }
  if (s[0] === "'") return s.slice(1, -1).replace(/''/g, "'");
  if (/^[+-]?\d+$/.test(s)) return parseInt(s, 10);
  if (/^[+-]?\d*\.\d+$/.test(s)) return parseFloat(s);
  return s;
}

interface Line {
  indent: number;
  text: string;
}

// Tokenize into (indent, text) lines, dropping blanks and full-line comments.
function tokenize(src: string): Line[] {
  const out: Line[] = [];
  for (const rawLine of src.split("\n")) {
    const line = rawLine.replace(/\t/g, "  ");
    if (!line.trim() || line.trim().startsWith("#")) continue;
    const indent = line.length - line.trimStart().length;
    out.push({ indent, text: line.trim() });
  }
  return out;
}

// Recursive-descent over the indentation-grouped lines.
function parseBlock(lines: Line[], start: number, indent: number): [Json, number] {
  // List?
  if (start < lines.length && lines[start].indent === indent && lines[start].text.startsWith("- ")) {
    const arr: Json[] = [];
    let i = start;
    while (i < lines.length && lines[i].indent === indent && lines[i].text.startsWith("- ")) {
      const inline = lines[i].text.slice(2).trim();
      if (inline.includes(":") && !inline.startsWith('"') && !inline.startsWith("'")) {
        // "- key: value" → start of a map item; re-read this + following deeper lines as a map.
        const synthetic: Line[] = [{ indent: indent + 2, text: inline }];
        let j = i + 1;
        while (j < lines.length && lines[j].indent > indent) {
          synthetic.push(lines[j]);
          j++;
        }
        const [obj] = parseBlock(synthetic, 0, indent + 2);
        arr.push(obj);
        i = j;
      } else {
        arr.push(inline === "" ? null : parseScalar(inline));
        i++;
      }
    }
    return [arr, i];
  }

  // Map.
  const obj: Record<string, Json> = {};
  let i = start;
  while (i < lines.length && lines[i].indent === indent && !lines[i].text.startsWith("- ")) {
    const text = lines[i].text;
    const colon = splitKey(text);
    if (!colon) throw new Error(`invalid line: ${text}`);
    const [key, rest] = colon;
    if (rest !== "") {
      obj[key] = parseScalar(rest);
      i++;
    } else {
      // Nested block on the following deeper lines.
      const childIndent = i + 1 < lines.length ? lines[i + 1].indent : indent + 2;
      if (childIndent > indent && i + 1 < lines.length) {
        const [child, next] = parseBlock(lines, i + 1, childIndent);
        obj[key] = child;
        i = next;
      } else {
        obj[key] = "";
        i++;
      }
    }
  }
  return [obj, i];
}

// Split "key: value" honoring a quoted value that may contain a colon.
function splitKey(text: string): [string, string] | null {
  const m = /^([^:\s][^:]*):(?:\s+(.*))?$/.exec(text);
  if (!m) return null;
  return [m[1].trim(), (m[2] ?? "").trim()];
}

export function parse(text: string): Json {
  const lines = tokenize(text);
  if (lines.length === 0) return {};
  const baseIndent = lines[0].indent;
  const [doc] = parseBlock(lines, 0, baseIndent);
  return doc;
}

export const yaml = { dump, parse };
