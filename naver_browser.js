"use strict";

// One headless Chromium profile stays open between searches for this process.
// Emit route details first, then a screenshot from that same page.
const { chromium } = require(process.env.NAVER_PLAYWRIGHT_MODULE || "playwright");
const readline = require("readline");

const profilePath = process.env.NAVER_PROFILE_PATH;

function emit(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

async function selectAddress(page, position, address) {
  const container = page.locator(`.search_input_box_wrap.${position}`);
  const clean = address.replace(/\([^)]*\)/g, " ").replace(/\s+/g, " ").trim();
  const road = clean.match(/^(.+?(?:로\d*길|로|길)\s*\d+(?:-\d+)?)(?:\s|$)/)?.[1];
  const queries = [...new Set([address, clean, road].filter(Boolean))];
  for (const query of queries) {
    await container.locator("input.input_search").fill(query);
    // Naver uses several suggestion item types depending on the address and
    // region. Select the first instant-search result regardless of type.
    const place = container.locator("[id^='instant-search-item-']").first();
    try {
      await place.waitFor({ state: "visible", timeout: 7000 });
      const selectedText = (await place.innerText()).replace(/\s+/g, " ").trim();
      await place.click();
      return selectedText;
    } catch (error) {
      if (query === queries[queries.length - 1]) {
        console.error(`Naver address suggestion lookup failed for ${position}: ${query}`);
        throw new Error(`Naver Maps found no selectable place for: ${address}`);
      }
    }
  }
}

async function waitForMap(page, mapClip) {
  await page.evaluate(() => document.fonts.ready);
  // Naver renders the route card before its map tiles and route overlay finish.
  // Require the visible map images to load and the map pixels to stop changing.
  let previous;
  let stableSamples = 0;
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const imagesReady = await page.evaluate(({ x, y, width, height }) =>
      [...document.images].every((image) => {
        const box = image.getBoundingClientRect();
        const onMap = box.width > 0 && box.height > 0 && box.left < x + width
          && box.right > x && box.top < y + height && box.bottom > y;
        return !onMap || (image.complete && image.naturalWidth > 0);
      }), mapClip);
    const current = await page.screenshot({ type: "png", clip: mapClip, timeout: 15000 });
    stableSamples = imagesReady && previous?.equals(current) ? stableSamples + 1 : 0;
    if (stableSamples >= 2) return;
    previous = current;
    await page.waitForTimeout(800);
  }
  throw new Error("Naver route loaded, but the map did not finish rendering");
}

async function runRoute(page, destination, startAddress) {
    if (!destination) throw new Error("Destination address is required");
    if (!startAddress) throw new Error("Starting address is required");
    page.setDefaultTimeout(12000);
    await page.goto("https://map.naver.com/p/directions/-/-/-/transit", {
      waitUntil: "domcontentloaded",
      timeout: 25000,
    });
    const selectedStart = await selectAddress(page, "start", startAddress);
    const selectedGoal = await selectAddress(page, "goal", destination);
    const card = page.locator("[class*=StyledPubtransDirectionsSummaryItem]").first();
    await card.waitFor({ state: "hidden", timeout: 5000 });
    await page.getByRole("button", { name: "길찾기", exact: true }).click();

    try {
      await Promise.race([
        card.waitFor({ state: "visible", timeout: 25000 }),
        page.locator("[class*=StyledDirectionsError]").first().waitFor({ state: "visible", timeout: 25000 })
          .then(() => { throw new Error("Naver Maps could not load the public-transit route"); }),
      ]);
    } catch (error) {
      throw new Error(`Naver could not load a public-transit route from ${selectedStart} to ${selectedGoal}: ${error.message.split("\n")[0]}`);
    }

    const details = await card.evaluate((root, destinationAddress) => {
      const clean = (value) => (value || "").trim().replace(/\s+/g, " ");
      const duration = (value) => clean(value).replace(/(\d+)\s*시간/g, "$1hr ").replace(/(\d+)\s*분/g, "$1min").replace(/\s+/g, " ").trim();
      const timeline = [...root.querySelectorAll("li[class*=StyledPubtransDirectionsTimelineItem]")];
      const walking = timeline.filter((item) => item.className.includes("_type_walking"));
      const walkTotal = walking.reduce((sum, item) => sum + Number((item.querySelector("[class*=pubtrans_time_taken]")?.textContent || "").match(/\d+/)?.[0] || 0), 0);
      const transitTimes = timeline.filter((item) => !item.className.includes("_type_walking"))
        .map((item) => duration(item.querySelector("[class*=pubtrans_time_taken]")?.textContent || ""));
      const nodes = [...root.querySelectorAll("li[class*=StyledPubtransDirectionsSummaryStep]")]
        .filter((item) => item.querySelector(".summary_step_icon_area > .summary_icon_label"));
      const steps = [];
      let destinationStop = "";
      for (const node of nodes) {
        const mode = clean(node.querySelector(".summary_step_icon_area > .summary_icon_label")?.textContent);
        const station = clean(node.querySelector(".citystep_station")?.textContent || node.querySelector(".step_title")?.textContent);
        if (mode === "하차") { destinationStop = station; continue; }
        if (!station) continue;
        const busCandidates = [...node.querySelectorAll(".wrap_remaining_bus .summary_icon_label, .alt_bus_arrival_number")]
          .map((item) => clean(item.textContent)).filter((item) => /^(?=.*\d)[가-힣A-Za-z0-9-]+$/.test(item));
        const bus = busCandidates[0] || "";
        steps.push({ label: bus ? `${mode} ${bus}` : mode, station, duration: transitTimes[steps.length] || "" });
      }
      if (!steps.length) {
        const intercity = [...root.querySelectorAll("li[class*=StyledPubtransDirectionsSummaryIntercityStep]")];
        for (const node of intercity) {
          const lines = node.innerText.split("\n").map(clean).filter(Boolean);
          const modeIndex = lines.findIndex((line) => /지하철|버스|기차/.test(line));
          if (modeIndex < 0) continue;
          const stops = [...node.querySelectorAll(".item_intercity_step")].map((item) => clean(item.textContent));
          const station = stops.length > 1 ? stops.join(" → ") : lines[0];
          const timeMatch = node.innerText.match(/(?:\d+\s*시간\s*)?\d+\s*분/);
          steps.push({
            label: clean([lines[modeIndex], lines[modeIndex + 1]].join(" ")),
            station,
            duration: timeMatch ? duration(timeMatch[0]) : "",
          });
        }
      }
      return {
        timespan: duration(root.querySelector(".wrap_time_taken")?.textContent),
        walk_total: `${walkTotal}min`,
        steps,
        destination: destinationStop || destinationAddress,
      };
    }, destination);
    if (!details.timespan || !details.steps.length) {
      throw new Error("Naver route loaded, but its transit steps could not be read");
    }
    emit({ type: "route", web_url: page.url(), details });

    const viewport = page.viewportSize();
    const directionsForm = await page.locator(".search_input_box_wrap.start").boundingBox();
    if (!viewport || !directionsForm) {
      throw new Error("Naver directions form disappeared before the map screenshot");
    }
    // Keep the results panel and map; trim the top tabs and outer map controls.
    const left = Math.max(0, Math.floor(directionsForm.x) - 12);
    const top = Math.max(0, Math.floor(directionsForm.y) - 12);
    const right = viewport.width - 60;
    const clip = { x: left, y: top, width: right - left, height: viewport.height - top };
    const mapClip = {
      x: Math.min(right - 100, Math.ceil(directionsForm.x + directionsForm.width + 20)),
      y: top,
      width: Math.max(100, right - Math.ceil(directionsForm.x + directionsForm.width + 20)),
      height: viewport.height - top,
    };
    await waitForMap(page, mapClip);
    const png = await page.screenshot({ type: "png", clip, timeout: 15000 });
    emit({ type: "screenshot", screenshot_data: png.toString("base64") });
}

async function main() {
  if (!profilePath) throw new Error("NAVER_PROFILE_PATH is required");
  const launchOptions = {
    headless: process.env.NAVER_HEADLESS !== "0",
    locale: "ko-KR",
    viewport: { width: 1560, height: 1100 },
    args: [
      "--no-first-run",
      "--no-default-browser-check",
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
    ],
  };
  // On Render, Playwright supplies Chromium. A custom executable is optional
  // for local Edge/Chrome installations and must never default to a Windows path.
  if (process.env.NAVER_EDGE_PATH) launchOptions.executablePath = process.env.NAVER_EDGE_PATH;
  const context = await chromium.launchPersistentContext(profilePath, launchOptions);
  if (!context.pages().length) await context.newPage();
  emit({ type: "ready" });
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of input) {
    let request;
    try { request = JSON.parse(line); } catch { emit({ type: "error", error: "Invalid route request" }); emit({ type: "complete" }); continue; }
    const watchdog = setTimeout(() => {
      emit({ type: "error", error: "Naver Maps browser timed out" });
      process.exit(1);
    }, 75000);
    let page;
    try {
      // A new tab starts without the previous request's visible result card.
      // The persistent context still shares its Naver login and cache.
      page = await context.newPage();
      await runRoute(page, request.address, request.start_address);
    } catch (error) {
      emit({ type: "error", error: error.message });
    } finally {
      if (page && !page.isClosed()) await page.close().catch(() => {});
      clearTimeout(watchdog);
      emit({ type: "complete" });
    }
  }
  await context.close();
}

main().catch((error) => {
  emit({ type: "error", error: error.message });
  process.exitCode = 1;
});
