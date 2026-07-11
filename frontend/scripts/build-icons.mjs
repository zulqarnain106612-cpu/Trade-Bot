// Rasterizes public/icon.svg into build/icon.png (512x512) plus two small
// tray-sized variants (plain + red-dot "pending approvals" badge) that
// electron/main.cjs swaps between at runtime. electron-builder derives
// .ico / .icns / all Linux sizes from the single 512px icon.png.
import sharp from "sharp";
import { mkdirSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, "..");
const svgPath = path.join(root, "public", "icon.svg");
const outDir = path.join(root, "build");

if (!existsSync(outDir)) mkdirSync(outDir, { recursive: true });

const svgBuffer = await sharp(svgPath).resize(512, 512).png().toBuffer();
await sharp(svgBuffer).toFile(path.join(outDir, "icon.png"));

const traySize = 32;
const trayBase = await sharp(svgBuffer).resize(traySize, traySize).png().toBuffer();
await sharp(trayBase).toFile(path.join(outDir, "tray.png"));

const dotRadius = 6;
const dotSvg = Buffer.from(
  `<svg xmlns="http://www.w3.org/2000/svg" width="${traySize}" height="${traySize}">
    <circle cx="${traySize - dotRadius - 1}" cy="${dotRadius + 1}" r="${dotRadius}"
      fill="#ef4444" stroke="#08070a" stroke-width="2"/>
  </svg>`
);
await sharp(trayBase)
  .composite([{ input: dotSvg, top: 0, left: 0 }])
  .toFile(path.join(outDir, "tray-badge.png"));

console.log(`Wrote ${outDir}/icon.png, tray.png, tray-badge.png`);
