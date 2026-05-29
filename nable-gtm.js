const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, BorderStyle, WidthType, ShadingType,
  LevelFormat, ExternalHyperlink, PageNumber, Header, Footer,
  TabStopType, TabStopPosition
} = require("docx");
const fs = require("fs");

const ACCENT = "E4A76B";
const DARK = "15140F";
const MID = "4A4639";
const LIGHT_BG = "F9F7F3";
const BORDER_COLOR = "D9D5C8";

const border = { style: BorderStyle.SINGLE, size: 1, color: BORDER_COLOR };
const borders = { top: border, bottom: border, left: border, right: border };
const noBorder = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };

function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 400, after: 160 },
    children: [new TextRun({ text, bold: true, size: 36, font: "Arial", color: DARK })]
  });
}

function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 320, after: 120 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: ACCENT, space: 4 } },
    children: [new TextRun({ text, bold: true, size: 26, font: "Arial", color: DARK })]
  });
}

function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 240, after: 80 },
    children: [new TextRun({ text, bold: true, size: 22, font: "Arial", color: MID })]
  });
}

function body(text, opts = {}) {
  return new Paragraph({
    spacing: { before: 80, after: 80 },
    children: [new TextRun({ text, size: 22, font: "Arial", color: DARK, ...opts })]
  });
}

function bullet(text, bold_prefix = "") {
  const children = [];
  if (bold_prefix) {
    children.push(new TextRun({ text: bold_prefix + " ", bold: true, size: 22, font: "Arial", color: DARK }));
    children.push(new TextRun({ text, size: 22, font: "Arial", color: DARK }));
  } else {
    children.push(new TextRun({ text, size: 22, font: "Arial", color: DARK }));
  }
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { before: 60, after: 60 },
    children
  });
}

function subbullet(text) {
  return new Paragraph({
    numbering: { reference: "subbullets", level: 1 },
    spacing: { before: 40, after: 40 },
    children: [new TextRun({ text, size: 20, font: "Arial", color: MID })]
  });
}

function spacer(lines = 1) {
  return new Paragraph({ children: [new TextRun("")], spacing: { before: 80 * lines, after: 0 } });
}

function callout(label, text) {
  return new Table({
    width: { size: 9360, type: WidthType.DXA },
    columnWidths: [1200, 8160],
    borders: { insideH: noBorder, insideV: noBorder },
    rows: [new TableRow({
      children: [
        new TableCell({
          borders: { top: border, bottom: border, left: border, right: noBorder },
          shading: { fill: "E4A76B", type: ShadingType.CLEAR },
          width: { size: 1200, type: WidthType.DXA },
          margins: { top: 120, bottom: 120, left: 160, right: 160 },
          verticalAlign: "center",
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [new TextRun({ text: label, bold: true, size: 18, font: "Arial", color: "FFFFFF" })]
          })]
        }),
        new TableCell({
          borders: { top: border, bottom: border, left: noBorder, right: border },
          shading: { fill: LIGHT_BG, type: ShadingType.CLEAR },
          width: { size: 8160, type: WidthType.DXA },
          margins: { top: 120, bottom: 120, left: 200, right: 160 },
          children: [new Paragraph({
            children: [new TextRun({ text, size: 20, font: "Arial", color: DARK })]
          })]
        })
      ]
    })]
  });
}

function weekTable(week, dateRange, actions, goal) {
  const headerRow = new TableRow({
    children: [
      new TableCell({
        borders,
        shading: { fill: DARK, type: ShadingType.CLEAR },
        width: { size: 2000, type: WidthType.DXA },
        margins: { top: 120, bottom: 120, left: 160, right: 160 },
        children: [
          new Paragraph({ children: [new TextRun({ text: week, bold: true, size: 22, font: "Arial", color: "FFFFFF" })] }),
          new Paragraph({ children: [new TextRun({ text: dateRange, size: 18, font: "Arial", color: ACCENT })] })
        ]
      }),
      new TableCell({
        borders,
        shading: { fill: DARK, type: ShadingType.CLEAR },
        width: { size: 5760, type: WidthType.DXA },
        margins: { top: 120, bottom: 120, left: 160, right: 160 },
        children: [new Paragraph({ children: [new TextRun({ text: "Actions", bold: true, size: 20, font: "Arial", color: "FFFFFF" })] })]
      }),
      new TableCell({
        borders,
        shading: { fill: DARK, type: ShadingType.CLEAR },
        width: { size: 1600, type: WidthType.DXA },
        margins: { top: 120, bottom: 120, left: 160, right: 160 },
        children: [new Paragraph({ children: [new TextRun({ text: "Goal", bold: true, size: 20, font: "Arial", color: "FFFFFF" })] })]
      })
    ]
  });

  const actionChildren = actions.map(a =>
    new Paragraph({
      numbering: { reference: "bullets", level: 0 },
      spacing: { before: 40, after: 40 },
      children: [new TextRun({ text: a, size: 20, font: "Arial", color: DARK })]
    })
  );

  const dataRow = new TableRow({
    children: [
      new TableCell({ borders, width: { size: 2000, type: WidthType.DXA }, margins: { top: 120, bottom: 120, left: 160, right: 160 }, children: [] }),
      new TableCell({ borders, width: { size: 5760, type: WidthType.DXA }, margins: { top: 120, bottom: 120, left: 160, right: 160 }, children: actionChildren }),
      new TableCell({
        borders,
        width: { size: 1600, type: WidthType.DXA },
        margins: { top: 120, bottom: 120, left: 160, right: 160 },
        children: [new Paragraph({ children: [new TextRun({ text: goal, size: 20, font: "Arial", color: MID, italics: true })] })]
      })
    ]
  });

  return new Table({
    width: { size: 9360, type: WidthType.DXA },
    columnWidths: [2000, 5760, 1600],
    rows: [headerRow, dataRow]
  });
}

function metricRow(metric, target, how) {
  return new TableRow({
    children: [
      new TableCell({ borders, width: { size: 3000, type: WidthType.DXA }, margins: { top: 100, bottom: 100, left: 140, right: 140 },
        children: [new Paragraph({ children: [new TextRun({ text: metric, bold: true, size: 20, font: "Arial", color: DARK })] })] }),
      new TableCell({ borders, width: { size: 2160, type: WidthType.DXA }, margins: { top: 100, bottom: 100, left: 140, right: 140 },
        shading: { fill: LIGHT_BG, type: ShadingType.CLEAR },
        children: [new Paragraph({ children: [new TextRun({ text: target, size: 20, font: "Arial", color: DARK })] })] }),
      new TableCell({ borders, width: { size: 4200, type: WidthType.DXA }, margins: { top: 100, bottom: 100, left: 140, right: 140 },
        children: [new Paragraph({ children: [new TextRun({ text: how, size: 20, font: "Arial", color: MID })] })] })
    ]
  });
}

const doc = new Document({
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [{
          level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 560, hanging: 280 } } }
        }]
      },
      {
        reference: "subbullets",
        levels: [
          { level: 0, format: LevelFormat.BULLET, text: "", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 0, hanging: 0 } } } },
          {
            level: 1, format: LevelFormat.BULLET, text: "◦", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 1000, hanging: 280 } } }
          }
        ]
      }
    ]
  },
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: "Arial", color: DARK },
        paragraph: { spacing: { before: 400, after: 160 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: "Arial", color: DARK },
        paragraph: { spacing: { before: 320, after: 120 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, font: "Arial", color: MID },
        paragraph: { spacing: { before: 240, after: 80 }, outlineLevel: 2 } }
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
      }
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
          border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: ACCENT, space: 4 } },
          children: [
            new TextRun({ text: "nable", bold: true, size: 18, font: "Arial", color: DARK }),
            new TextRun({ text: "\tGTM Game Plan 2026", size: 18, font: "Arial", color: MID })
          ]
        })]
      })
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
          border: { top: { style: BorderStyle.SINGLE, size: 4, color: BORDER_COLOR, space: 4 } },
          children: [
            new TextRun({ text: "Confidential", size: 16, font: "Arial", color: MID }),
            new TextRun({ text: "\tPage ", size: 16, font: "Arial", color: MID }),
            new TextRun({ children: [PageNumber.CURRENT], size: 16, font: "Arial", color: MID })
          ]
        })]
      })
    },
    children: [
      // Cover block
      new Paragraph({
        spacing: { before: 480, after: 80 },
        children: [new TextRun({ text: "nable", bold: true, size: 72, font: "Arial", color: DARK })]
      }),
      new Paragraph({
        spacing: { before: 0, after: 80 },
        children: [new TextRun({ text: "Go-to-Market Game Plan", size: 36, font: "Arial", color: MID })]
      }),
      new Paragraph({
        spacing: { before: 0, after: 0 },
        children: [new TextRun({ text: "June 2026  ·  Chandan Bukkapatnam  ·  getnable.com", size: 20, font: "Arial", color: MID })]
      }),
      new Paragraph({
        spacing: { before: 40, after: 400 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: ACCENT, space: 8 } },
        children: []
      }),

      // Situation
      h2("Where We Are"),
      body("nable is a local-first FinOps MCP server. It connects Claude, Cursor, and any MCP client to AWS, Azure, GCP, and 14 SaaS providers. Users ask questions in plain English and get real cost data, anomaly alerts, and rightsizing recommendations back instantly."),
      spacer(),
      body("Current state:"),
      bullet("4,264 PyPI installs per month, growing week over week"),
      bullet("Pricing: free tier forever, Team plan at $39.99/user/month with 7-day trial"),
      bullet("Webhook, license delivery, and onboarding flow fully wired"),
      bullet("Zero paid customers yet"),
      spacer(),
      callout("THESIS", "The product is ready. This is a pure distribution problem. Every action this month should drive installs or conversations with potential customers."),
      spacer(2),

      // ICP
      h2("Who We Are Selling To"),
      body("Primary: Platform engineers and DevOps leads at Series A to Series C companies. They own the cloud bill, they use AI coding tools daily, and they are annoyed by the existing FinOps tooling that is either too expensive or too enterprise-focused."),
      spacer(),
      body("Secondary: FinOps practitioners and engineering managers who get the quarterly \"why is our AWS bill up\" email and have to explain it."),
      spacer(),
      body("Not our customer right now: CFOs, finance teams, large enterprise procurement. That comes later."),
      spacer(),
      callout("ICP SIGNAL", "If someone has \"platform engineering\", \"DevOps\", \"SRE\", or \"FinOps\" in their LinkedIn title and works at a 50-500 person tech company, they are the buyer."),
      spacer(2),

      // Channels
      h2("Channels and Why"),
      body("We have no budget for paid acquisition. Everything is organic. Rank channels by how close they are to the ICP and how fast they convert."),
      spacer(),

      h3("1. Community (highest leverage, this week)"),
      bullet("Anthropic Discord", "#mcp-servers:"),
      subbullet("Developers actively searching for new MCP servers. Post a demo and install command. Do not pitch, just show the tool working."),
      bullet("Reddit", "r/aws, r/devops, r/LocalLLaMA:"),
      subbullet("\"I built this because I was annoyed\" framing outperforms ads by 10x on Reddit. Show real terminal output. No marketing language."),
      bullet("GitHub", "Awesome-MCP lists:"),
      subbullet("Search for MCP server directories and awesome lists. Open a PR to add nable. These get indexed and discovered passively."),
      spacer(),

      h3("2. Social (this week, after demo video is ready)"),
      bullet("Twitter/X: One post with a 45-second screen recording. Tag @AnthropicAI and @cursor_ai. MCP content is getting organic reach right now."),
      bullet("LinkedIn: Founder story post. \"I spent 6 months switching between the AWS console and Claude. Here is what I built.\" Target: platform engineers and FinOps practitioners."),
      bullet("Post PyPI growth weekly. Growth content performs well with a technical audience and builds credibility over time."),
      spacer(),

      h3("3. Product Hunt (week 2)"),
      bullet("Post Tuesday or Wednesday. Live before 12:01am PT."),
      bullet("Need the demo video before posting. Without it, PH traffic does not convert."),
      bullet("Primary value: social proof, backlink, and a spike of developer installs. Not a revenue event."),
      spacer(),

      h3("4. Direct outreach (ongoing)"),
      bullet("10 cold messages per week on LinkedIn to platform engineers."),
      bullet("Not a pitch. Ask for feedback: \"Built this, would love to know if it solves a problem you actually have.\""),
      bullet("One reply that converts to a call is worth more than 100 Product Hunt upvotes."),
      spacer(2),

      // Demo video
      h2("The Demo Video (Blocker for Everything Else)"),
      callout("PRIORITY", "Do this before anything else. The video unlocks PH, Twitter, and LinkedIn simultaneously."),
      spacer(),
      body("Format: 45-60 seconds. Screen recording. No voiceover. Moody music. Text overlays."),
      spacer(),
      body("Shot sequence:"),
      bullet("3 seconds: AWS console open, large bill number visible"),
      bullet("Cut to: Claude open, question being typed slowly: \"What drove our costs up this month?\""),
      bullet("Cut to: nable response streaming with real breakdown by service and team"),
      bullet("Cut to: \"Create a Jira ticket for the top anomaly\" typed"),
      bullet("Cut to: Jira ticket appearing with title, priority, and savings amount"),
      bullet("Text overlay: \"Your cloud bill. In your editor.\""),
      bullet("End card: getnable.com  ·  pip install finops-mcp"),
      spacer(),
      body("Tools:"),
      bullet("ScreenStudio ($40) for the cinematic zoom and device frame. This is what most YC demo videos use."),
      bullet("CapCut or DaVinci Resolve (free) for cuts and text overlays."),
      bullet("Use real data. Do not fake the numbers. Authenticity is the whole point."),
      spacer(2),

      // Product Hunt
      h2("Product Hunt Listing"),
      h3("Tagline"),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [9360],
        rows: [new TableRow({
          children: [new TableCell({
            borders,
            shading: { fill: LIGHT_BG, type: ShadingType.CLEAR },
            width: { size: 9360, type: WidthType.DXA },
            margins: { top: 160, bottom: 160, left: 240, right: 240 },
            children: [new Paragraph({
              children: [new TextRun({ text: "Ask Claude about your cloud bill in plain English", bold: true, size: 24, font: "Arial", color: DARK, italics: true })]
            })]
          })]
        })]
      }),
      spacer(),
      h3("Description"),
      body("nable is an MCP server that connects Claude, Cursor, and Windsurf to your AWS, Azure, GCP, and SaaS costs. Ask questions in plain English. Get real answers. Your data never leaves your machine."),
      spacer(),
      body("What you can do:"),
      bullet("\"What drove our AWS costs up 40% this month?\""),
      bullet("\"Which team is spending the most on Datadog?\""),
      bullet("\"Create a Jira ticket for any EC2 waste over $200/mo\""),
      bullet("\"What will our bill look like next month?\""),
      spacer(),
      body("Supports 17 providers. Free tier forever. One command to install."),
      spacer(),
      h3("First Comment (post this yourself on launch day)"),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [9360],
        rows: [new TableRow({
          children: [new TableCell({
            borders,
            shading: { fill: LIGHT_BG, type: ShadingType.CLEAR },
            width: { size: 9360, type: WidthType.DXA },
            margins: { top: 160, bottom: 160, left: 240, right: 240 },
            children: [
              new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: "Hey PH, Chandan here, the founder.", size: 22, font: "Arial", color: DARK })] }),
              new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: "I built nable because I spent most of my day bouncing between the AWS console, six dashboards that barely showed what I needed, and Claude. I would manually paste numbers into Claude, get an answer, then go back and repeat the whole thing.", size: 22, font: "Arial", color: DARK })] }),
              new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: "Every FinOps tool coming out right now is built for enterprise and priced for enterprise. None of them fit the way an actual engineer works.", size: 22, font: "Arial", color: DARK })] }),
              new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: "nable runs locally. Credentials stay in your OS keyring. Nothing leaves your machine. One command to install: pip install finops-mcp && finops welcome", size: 22, font: "Arial", color: DARK })] }),
              new Paragraph({ spacing: { before: 0, after: 0 }, children: [new TextRun({ text: "Happy to answer any questions here. If it solves a real problem for you, I want to know what that problem is.", size: 22, font: "Arial", color: DARK })] })
            ]
          })]
        })]
      }),
      spacer(2),

      // Week by week
      h2("30-Day Execution Plan"),
      spacer(),
      weekTable(
        "Week 1",
        "June 2-8",
        [
          "Record the demo video (ScreenStudio, 45 seconds, real AWS data)",
          "Post in Anthropic Discord #mcp-servers",
          "Submit to 3 GitHub awesome-mcp lists via PR",
          "Post on r/aws: \"I built this because I was annoyed\"",
          "Verify end-to-end: test purchase with coupon, confirm license email arrives"
        ],
        "First 50 organic installs from demo"
      ),
      spacer(),
      weekTable(
        "Week 2",
        "June 9-15",
        [
          "Post on Twitter/X with demo video",
          "Post on LinkedIn with founder story",
          "Product Hunt launch (Tuesday or Wednesday)",
          "Start 10 cold LinkedIn messages to platform engineers",
          "Reply to every PH comment same day"
        ],
        "PH launch + 200 new installs"
      ),
      spacer(),
      weekTable(
        "Week 3",
        "June 16-22",
        [
          "Follow up with any PH commenters who showed real interest",
          "Post weekly PyPI growth stat on LinkedIn",
          "Post on r/devops and r/LocalLLaMA",
          "Convert one PH or Reddit conversation into a 30-minute call",
          "Post video on LinkedIn natively (not just a link)"
        ],
        "First paid customer or trial"
      ),
      spacer(),
      weekTable(
        "Week 4",
        "June 23-30",
        [
          "Follow up with all 40 cold outreach targets",
          "Write one honest blog post: \"What I learned shipping a FinOps MCP server\"",
          "Post on Hacker News Show HN if installs and feedback are strong",
          "Review PyPI data and identify which channels drove installs",
          "Double down on the one channel that worked"
        ],
        "10 paid trials or 1 paying team"
      ),
      spacer(2),

      // Metrics
      h2("Success Metrics"),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [3000, 2160, 4200],
        rows: [
          new TableRow({
            children: [
              new TableCell({ borders, shading: { fill: DARK, type: ShadingType.CLEAR }, width: { size: 3000, type: WidthType.DXA }, margins: { top: 100, bottom: 100, left: 140, right: 140 },
                children: [new Paragraph({ children: [new TextRun({ text: "Metric", bold: true, size: 20, font: "Arial", color: "FFFFFF" })] })] }),
              new TableCell({ borders, shading: { fill: DARK, type: ShadingType.CLEAR }, width: { size: 2160, type: WidthType.DXA }, margins: { top: 100, bottom: 100, left: 140, right: 140 },
                children: [new Paragraph({ children: [new TextRun({ text: "30-Day Target", bold: true, size: 20, font: "Arial", color: "FFFFFF" })] })] }),
              new TableCell({ borders, shading: { fill: DARK, type: ShadingType.CLEAR }, width: { size: 4200, type: WidthType.DXA }, margins: { top: 100, bottom: 100, left: 140, right: 140 },
                children: [new Paragraph({ children: [new TextRun({ text: "How to Measure", bold: true, size: 20, font: "Arial", color: "FFFFFF" })] })] })
            ]
          }),
          metricRow("PyPI installs/month", "8,000+", "pypistats.org/packages/finops-mcp"),
          metricRow("Paying customers", "3+", "Stripe dashboard"),
          metricRow("Active trials", "20+", "Stripe active subscriptions"),
          metricRow("Founder calls booked", "5+", "Direct calendar bookings"),
          metricRow("PH upvotes", "200+", "Product Hunt launch day"),
          metricRow("Repo stars", "100+", "GitHub repository")
        ]
      }),
      spacer(2),

      // What not to do
      h2("What Not to Do"),
      bullet("Do not spend time on paid ads. No budget and no conversion data yet."),
      bullet("Do not build new features to attract customers. The product is ready. Ship later, sell now."),
      bullet("Do not post on Product Hunt without the demo video. Traffic will not convert."),
      bullet("Do not pitch in cold outreach. Ask for feedback. Pitching gets ignored."),
      bullet("Do not spread across every channel at once. Find the one that works and double down."),
      spacer(2),

      // Links
      h2("Key Links"),
      bullet("Website: "),
      new Paragraph({
        numbering: { reference: "bullets", level: 0 },
        spacing: { before: 60, after: 60 },
        children: [new ExternalHyperlink({
          children: [new TextRun({ text: "getnable.com", size: 22, font: "Arial", style: "Hyperlink" })],
          link: "https://getnable.com"
        })]
      }),
      new Paragraph({
        numbering: { reference: "bullets", level: 0 },
        spacing: { before: 60, after: 60 },
        children: [
          new TextRun({ text: "PyPI stats: ", size: 22, font: "Arial", color: DARK }),
          new ExternalHyperlink({
            children: [new TextRun({ text: "pypistats.org/packages/finops-mcp", size: 22, font: "Arial", style: "Hyperlink" })],
            link: "https://pypistats.org/packages/finops-mcp"
          })
        ]
      }),
      new Paragraph({
        numbering: { reference: "bullets", level: 0 },
        spacing: { before: 60, after: 60 },
        children: [
          new TextRun({ text: "Stripe dashboard: ", size: 22, font: "Arial", color: DARK }),
          new ExternalHyperlink({
            children: [new TextRun({ text: "dashboard.stripe.com", size: 22, font: "Arial", style: "Hyperlink" })],
            link: "https://dashboard.stripe.com"
          })
        ]
      }),
      new Paragraph({
        numbering: { reference: "bullets", level: 0 },
        spacing: { before: 60, after: 60 },
        children: [
          new TextRun({ text: "Contact: ", size: 22, font: "Arial", color: DARK }),
          new TextRun({ text: "chandanirving@gmail.com", size: 22, font: "Arial", color: DARK })
        ]
      }),
      spacer(2),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { before: 400, after: 0 },
        border: { top: { style: BorderStyle.SINGLE, size: 4, color: ACCENT, space: 8 } },
        children: [new TextRun({ text: "Ship is ready. Now go find the people who need it.", size: 22, font: "Arial", color: MID, italics: true })]
      })
    ]
  }]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("/Users/chandan/finops/nable-gtm.docx", buffer);
  console.log("Done: nable-gtm.docx");
});
