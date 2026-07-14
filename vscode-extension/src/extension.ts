/**
 * nable VS Code Extension — Cloud Cost Estimates for Terraform
 *
 * Shows estimated monthly AWS costs as inline ghost text and
 * CodeLens above every `resource` block in .tf files.
 *
 * Features:
 *   - Inline decoration: "$560/mo · m5.4xlarge on-demand"
 *   - CodeLens: summarises total file cost above first resource
 *   - Hover: detailed breakdown + savings tips
 *   - File summary command: total cost of all resources in file
 *   - Zero network calls: all pricing data is embedded
 */

import * as vscode from "vscode";
import { parseResources, ResourceBlock } from "./parser";
import { priceResource, formatMonthly, PriceEntry, HOURS_PER_MONTH } from "./prices";

// ── Decoration type ────────────────────────────────────────────────────────────

function makeDecorationType(color: string) {
  return vscode.window.createTextEditorDecorationType({
    after: {
      margin: "0 0 0 2em",
      color,
      fontStyle: "italic",
      fontWeight: "normal",
    },
  });
}

let decorationHigh:    vscode.TextEditorDecorationType;
let decorationMedium:  vscode.TextEditorDecorationType;
let decorationLow:     vscode.TextEditorDecorationType;
let decorationFree:    vscode.TextEditorDecorationType;
let decorationWarning: vscode.TextEditorDecorationType;

function initDecorationTypes() {
  decorationHigh    = makeDecorationType("#e05252cc");   // red — expensive
  decorationMedium  = makeDecorationType("#e0a050cc");   // amber — moderate
  decorationLow     = makeDecorationType("#6db06dcc");   // green — cheap
  decorationFree    = makeDecorationType("#888888aa");   // grey — pay-per-use
  decorationWarning = makeDecorationType("#cc8800cc");   // orange — has a savings tip
}

function decTypeForEntry(entry: PriceEntry): vscode.TextEditorDecorationType {
  if (entry.note) return decorationWarning;
  if (entry.monthly === 0) return decorationFree;
  if (entry.monthly >= 500) return decorationHigh;
  if (entry.monthly >= 100) return decorationMedium;
  return decorationLow;
}

// ── CodeLens provider ─────────────────────────────────────────────────────────

class CostCodeLensProvider implements vscode.CodeLensProvider {
  private _onDidChangeCodeLenses = new vscode.EventEmitter<void>();
  onDidChangeCodeLenses = this._onDidChangeCodeLenses.event;

  refresh() { this._onDidChangeCodeLenses.fire(); }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    if (!getCfg().enabled) return [];
    const text = document.getText();
    const blocks = parseResources(text);
    if (blocks.length === 0) return [];

    const cfg = getCfg();
    let totalMonthly = 0;
    let priced = 0;
    let unpriced = 0;

    for (const block of blocks) {
      const entry = priceResource(block.resourceType, block.attrs);
      if (entry) {
        totalMonthly += entry.monthly;
        priced++;
      } else {
        unpriced++;
      }
    }

    const totalAnnual = totalMonthly * 12;
    const label =
      `☁ nable estimate: $${totalMonthly.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}/mo` +
      (cfg.showAnnual ? `  ($${totalAnnual.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}/yr)` : "") +
      ` · ${priced} resource${priced !== 1 ? "s" : ""} priced` +
      (unpriced > 0 ? `, ${unpriced} unpriced` : "");

    const range = new vscode.Range(blocks[0].headerLine, 0, blocks[0].headerLine, 0);
    const lens = new vscode.CodeLens(range, {
      title: label,
      command: "nable.showSummary",
      arguments: [document.uri],
    });
    return [lens];
  }
}

// ── Hover provider ────────────────────────────────────────────────────────────

class CostHoverProvider implements vscode.HoverProvider {
  provideHover(
    document: vscode.TextDocument,
    position: vscode.Position,
  ): vscode.Hover | null {
    if (!getCfg().enabled) return null;

    const text = document.getText();
    const blocks = parseResources(text);

    const block = blocks.find(
      (b) => position.line >= b.startLine && position.line <= b.closingLine
    );
    if (!block) return null;

    const entry = priceResource(block.resourceType, block.attrs);

    const md = new vscode.MarkdownString();
    md.isTrusted = true;
    md.appendMarkdown(`**nable cost estimate** — \`${block.resourceType}.${block.resourceName}\`\n\n`);

    if (!entry) {
      md.appendMarkdown(`_No pricing data for \`${block.resourceType}\`_\n\n`);
      md.appendMarkdown(`[View AWS pricing →](https://aws.amazon.com/pricing/)`);
      return new vscode.Hover(md);
    }

    if (entry.monthly === 0) {
      md.appendMarkdown(`**Pay-per-use** — ${entry.detail}\n\n`);
    } else {
      const mo = entry.monthly;
      const yr = mo * 12;
      md.appendMarkdown(`| | |\n|---|---|\n`);
      md.appendMarkdown(`| **Monthly** | **$${mo.toLocaleString("en-US", { minimumFractionDigits: 2 })}** |\n`);
      md.appendMarkdown(`| Annual | $${yr.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })} |\n`);
      md.appendMarkdown(`| Detail | ${entry.detail} |\n`);
      md.appendMarkdown(`| Pricing | On-demand, us-east-1 |\n\n`);
    }

    if (entry.note) {
      md.appendMarkdown(`\n> 💡 **Savings tip:** ${entry.note}\n\n`);
    }

    md.appendMarkdown(`\n---\n_[nable finops](https://github.com/getnable/finopsmcp) · prices are estimates, us-east-1 on-demand_`);
    return new vscode.Hover(md);
  }
}

// ── Main decoration updater ───────────────────────────────────────────────────

function updateDecorations(editor: vscode.TextEditor) {
  const cfg = getCfg();
  if (!cfg.enabled) {
    clearAll(editor);
    return;
  }

  if (!editor.document.fileName.endsWith(".tf")) {
    clearAll(editor);
    return;
  }

  const text = editor.document.getText();
  const blocks = parseResources(text);

  const highRanges:    vscode.DecorationOptions[] = [];
  const mediumRanges:  vscode.DecorationOptions[] = [];
  const lowRanges:     vscode.DecorationOptions[] = [];
  const freeRanges:    vscode.DecorationOptions[] = [];
  const warnRanges:    vscode.DecorationOptions[] = [];

  for (const block of blocks) {
    const entry = priceResource(block.resourceType, block.attrs);
    if (!entry) continue;
    if (entry.monthly < cfg.minCostToShow && entry.monthly > 0) continue;

    const line = editor.document.lineAt(block.headerLine);
    const range = new vscode.Range(
      block.headerLine, line.text.length,
      block.headerLine, line.text.length,
    );

    const hoverMessage = new vscode.MarkdownString();
    if (entry.note) hoverMessage.appendMarkdown(`💡 ${entry.note}`);

    const decoration: vscode.DecorationOptions = {
      range,
      renderOptions: {
        after: { contentText: " " + formatMonthly(entry, cfg.showAnnual) },
      },
      hoverMessage: entry.note ? hoverMessage : undefined,
    };

    const bucket = decTypeForEntry(entry);
    if (bucket === decorationHigh)    highRanges.push(decoration);
    else if (bucket === decorationMedium) mediumRanges.push(decoration);
    else if (bucket === decorationLow)    lowRanges.push(decoration);
    else if (bucket === decorationWarning) warnRanges.push(decoration);
    else freeRanges.push(decoration);
  }

  editor.setDecorations(decorationHigh,    highRanges);
  editor.setDecorations(decorationMedium,  mediumRanges);
  editor.setDecorations(decorationLow,     lowRanges);
  editor.setDecorations(decorationFree,    freeRanges);
  editor.setDecorations(decorationWarning, warnRanges);
}

function clearAll(editor: vscode.TextEditor) {
  [decorationHigh, decorationMedium, decorationLow, decorationFree, decorationWarning]
    .forEach((d) => editor.setDecorations(d, []));
}

// ── Config helper ─────────────────────────────────────────────────────────────

interface Config {
  enabled: boolean;
  showAnnual: boolean;
  minCostToShow: number;
  region: string;
}

function getCfg(): Config {
  const cfg = vscode.workspace.getConfiguration("nable");
  return {
    enabled:       cfg.get<boolean>("enabled", true),
    showAnnual:    cfg.get<boolean>("showAnnual", false),
    minCostToShow: cfg.get<number>("minCostToShow", 1),
    region:        cfg.get<string>("region", "us-east-1"),
  };
}

// ── Extension lifecycle ───────────────────────────────────────────────────────

let codeLensProvider: CostCodeLensProvider;
let updateTimer: NodeJS.Timeout | undefined;

export function activate(context: vscode.ExtensionContext) {
  initDecorationTypes();

  codeLensProvider = new CostCodeLensProvider();
  const hoverProvider = new CostHoverProvider();

  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider(
      { language: "terraform", scheme: "file" },
      codeLensProvider,
    ),
    vscode.languages.registerHoverProvider(
      { language: "terraform", scheme: "file" },
      hoverProvider,
    ),
  );

  // Decorate active editor on open
  if (vscode.window.activeTextEditor) {
    updateDecorations(vscode.window.activeTextEditor);
  }

  // Debounced update on text change
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument((evt) => {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.document !== evt.document) return;
      if (updateTimer) clearTimeout(updateTimer);
      updateTimer = setTimeout(() => {
        updateDecorations(editor);
        codeLensProvider.refresh();
      }, 400);
    }),
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      if (editor) {
        updateDecorations(editor);
        codeLensProvider.refresh();
      }
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("nable")) {
        const editor = vscode.window.activeTextEditor;
        if (editor) updateDecorations(editor);
        codeLensProvider.refresh();
      }
    }),
  );

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand("nable.refreshCosts", () => {
      const editor = vscode.window.activeTextEditor;
      if (editor) updateDecorations(editor);
      codeLensProvider.refresh();
      vscode.window.showInformationMessage("nable: Cost estimates refreshed.");
    }),

    vscode.commands.registerCommand("nable.showSummary", (uri?: vscode.Uri) => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) return;
      const text = editor.document.getText();
      const blocks = parseResources(text);

      let totalMonthly = 0;
      const lines: string[] = [];

      for (const block of blocks) {
        const entry = priceResource(block.resourceType, block.attrs);
        if (!entry) {
          lines.push(`  ${block.resourceType}.${block.resourceName}  →  (unpriced)`);
          continue;
        }
        totalMonthly += entry.monthly;
        const cost = entry.monthly === 0
          ? "pay-per-use"
          : `$${entry.monthly.toFixed(2)}/mo`;
        const tip = entry.note ? `  ⚠ ${entry.note}` : "";
        lines.push(`  ${block.resourceType}.${block.resourceName}  →  ${cost}  [${entry.detail}]${tip}`);
      }

      const summary = [
        `nable Cost Summary — ${editor.document.fileName.split("/").pop()}`,
        `─`.repeat(60),
        ...lines,
        `─`.repeat(60),
        `  Total estimated: $${totalMonthly.toFixed(2)}/mo  ($${(totalMonthly * 12).toFixed(0)}/yr)`,
        `  Prices: AWS on-demand, us-east-1`,
      ].join("\n");

      const panel = vscode.window.createOutputChannel("nable Cost Summary");
      panel.clear();
      panel.appendLine(summary);
      panel.show();
    }),

    vscode.commands.registerCommand("nable.openDocs", () => {
      vscode.env.openExternal(vscode.Uri.parse("https://github.com/getnable/finopsmcp"));
    }),
  );
}

export function deactivate() {
  [decorationHigh, decorationMedium, decorationLow, decorationFree, decorationWarning]
    .forEach((d) => d.dispose());
}
