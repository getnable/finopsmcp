/**
 * Lightweight Terraform HCL parser — regex-based, no external deps.
 *
 * Handles:
 *   resource "aws_instance" "web" {
 *     instance_type = "m5.large"
 *     ami           = "ami-12345678"
 *   }
 *
 * Returns ResourceBlock[] with line numbers for decoration placement.
 */

export interface ResourceBlock {
  resourceType: string;          // "aws_instance"
  resourceName: string;          // "web"
  attrs: Record<string, string>; // { instance_type: "m5.large", ... }
  startLine: number;             // 0-indexed, line of "resource" keyword
  closingLine: number;           // 0-indexed, line of closing "}"
  headerLine: number;            // same as startLine (where we put the decoration)
}

// Match:  resource "aws_instance" "web" {
const RESOURCE_RE = /^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{/;

// Match simple assignments:  instance_type = "m5.large"
const ATTR_RE = /^\s+(\w+)\s*=\s*"([^"]+)"/;

// Match numeric assignments:  shard_count = 3
const ATTR_NUM_RE = /^\s+(\w+)\s*=\s*(\d+)/;

// Match boolean:  multi_az = true
const ATTR_BOOL_RE = /^\s+(\w+)\s*=\s*(true|false)/;

// Nested block opener (cluster_config { ...) — we descend one level to extract attrs
const NESTED_OPEN_RE = /^\s+\w+\s*\{/;

export function parseResources(text: string): ResourceBlock[] {
  const lines = text.split("\n");
  const blocks: ResourceBlock[] = [];

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const m = RESOURCE_RE.exec(line.trimStart());
    if (!m) { i++; continue; }

    const resourceType = m[1];
    const resourceName = m[2];
    const startLine = i;
    const attrs: Record<string, string> = {};

    // Scan forward for attributes until matching closing brace
    let depth = 1;
    i++;
    while (i < lines.length && depth > 0) {
      const l = lines[i];
      const trimmed = l.trim();

      if (trimmed === "{" || trimmed.endsWith("{")) depth++;
      if (trimmed === "}") {
        depth--;
        if (depth === 0) break;
        i++;
        continue;
      }

      // Extract string attributes
      const sa = ATTR_RE.exec(l);
      if (sa) { attrs[sa[1]] = sa[2]; i++; continue; }

      // Extract numeric attributes (store as string)
      const na = ATTR_NUM_RE.exec(l);
      if (na) { attrs[na[1]] = na[2]; i++; continue; }

      // Extract boolean attributes
      const ba = ATTR_BOOL_RE.exec(l);
      if (ba) { attrs[ba[1]] = ba[2]; i++; continue; }

      i++;
    }

    blocks.push({
      resourceType,
      resourceName,
      attrs,
      startLine,
      closingLine: i,
      headerLine: startLine,
    });

    i++;
  }

  return blocks;
}
