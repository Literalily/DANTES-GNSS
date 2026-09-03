// script.js

// for the baseline vector chart
let deviationChart = null;
const baselineSeries = new Map();
const latestByBaseline = new Map(); // Most recent point per baseline kept separately to always accurately represent current status

const COLOR_PALETTE = [
    [227, 6, 19],    // red
    [54, 162, 235],  // blue
    [46, 184, 92],   // green
    [255, 159, 64],  // orange
    [153, 102, 255], // purple
    [0, 188, 212],   // cyan
];
let nextColorIndex = 0;

// Safety cap for load times (oldest points are dropped once baseline passes this)
const MAX_CLIENT_POINTS = 5000;

/* =-=-=-=-= LOAD CHARTS =-=-=-=-= */
document.addEventListener("DOMContentLoaded", async () => {
    initChart();
    await loadInitialHistory(); // one-off fetch of everything recorded before this page loaded
    connectLiveStream(); // live push connection for everything after that

    loadDistanceStats(); // stats tables
    setInterval(loadDistanceStats, DISTANCE_STATS_REFRESH_MS);
})

/* =-=-=-=-= DECORATE CHART =-=-=-=-= */
const circularGridPlugin = {
    id: 'circularGrid',
    beforeDraw: (chart) => {
        const ctx = chart.ctx;
        const xAxis = chart.scales.x;
        const yAxis = chart.scales.y;

        const centreX = xAxis.getPixelForValue((xAxis.max + xAxis.min) / 2);
        const centreY = yAxis.getPixelForValue((yAxis.max + yAxis.min) / 2);

        const maxRadius = Math.min(xAxis.right - centreX, centreY - yAxis.top);
        const maxMetres = (xAxis.max - xAxis.min) / 2;

        ctx.save();
        ctx.strokeStyle = 'rgba(150, 150, 150, 0.3)';
        ctx.fillStyle = 'rgba(166, 35, 33, 0.9)'
        ctx.font = '11px Arial';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.lineWidth = 1;

        for (let i = 1; i <= 4; i++) {
            const r = maxRadius * (i / 4);
            ctx.beginPath();
            ctx.arc(centreX, centreY, r, 0, 2 * Math.PI);
            ctx.stroke();
            const ringMetres = (maxMetres * (i / 4)).toFixed(2);
            ctx.fillText(`${ringMetres}m`, centreX + r * Math.SQRT1_2 + 4, centreY - r * Math.SQRT1_2);
        }

        ctx.strokeStyle = 'rgba(0, 0, 0, 0.3)';
        ctx.beginPath();
        ctx.moveTo(centreX, yAxis.top);
        ctx.lineTo(centreX, yAxis.bottom);
        ctx.moveTo(xAxis.left, centreY);
        ctx.lineTo(xAxis.right, centreY);
        ctx.stroke();

        ctx.fillStyle = 'rgba(166, 35, 33, 0.9)';
        ctx.font = 'bold 13px Arial';
        ctx.textAlign = 'center';
        ctx.fillText('N', centreX, yAxis.top - 10);
        ctx.fillText('S', centreX, yAxis.bottom + 14);
        ctx.textAlign = 'left';
        ctx.fillText('E', xAxis.right + 6, centreY);
        ctx.textAlign = 'right';
        ctx.fillText('W', xAxis.left - 6, centreY);

        ctx.restore();
    }
};

/* =-=-=-=-= COLOUR HELPER =-=-=-=-= */
function fadeColor([r, g, b], index, total) {
    if (total <= 1) return `rgba(${r}, ${g}, ${b}, 1)`;
    const minOpacity = 0.25;
    const t = index / (total - 1);
    const opacity = minOpacity + (1 - minOpacity) * t;
    return `rgba(${r}, ${g}, ${b}, ${opacity.toFixed(3)})`;
}

// new baselines are assigned the next palette colour
function ensureBaseline(baselineName) {
    if (!baselineSeries.has(baselineName)) {
        const color = COLOR_PALETTE[nextColorIndex % COLOR_PALETTE.length];
        nextColorIndex++;
        baselineSeries.set(baselineName, { points: [], color });
    }
    return baselineSeries.get(baselineName);
}

// plots each baseline's E/N vector on the chart directly (no lat/lon conversion needed as RTKNAVI already outputs meters relative to the base receiver)
function computeSymmetricBounds(datasets) {
    let maxAbs = 0.1; // 10cm floor, grows automatically if a baseline drifts further
    datasets.forEach(ds => {
        ds.data.forEach(pt => {
            maxAbs = Math.max(maxAbs, Math.abs(pt.x || 0), Math.abs(pt.y || 0));
        });
    });
    return maxAbs * 1.3;
}

// rebuilds chart.data.datasets from baselineSeries
// one dataset per baseline, with older points faded out and the newest point drawn larger
function syncDatasetsFromBaselines() {
    const baselinesArray = Array.from(baselineSeries.entries());

    const datasets = baselinesArray.map(([name, series]) => {
        if (series.points.length === 0) return null;

        const mappedData = series.points.map(pt => ({
            x: pt.e,
            y: pt.n,
            u: pt.u,
            distance: pt.distance,
            q: pt.q,
            time: pt.time
        }));

        return {
            label: name,
            data: mappedData,
            showLine: true,
            borderWidth: 2,
            borderColor: `rgba(${series.color.join(', ')}, 0.35)`,
            segment: {
                borderColor: (ctx) => fadeColor(series.color, ctx.p0DataIndex, series.points.length)
            },
            pointBackgroundColor: (ctx) => fadeColor(series.color, ctx.dataIndex, series.points.length),
            pointBorderWidth: 0,
            pointRadius: (ctx) => ctx.dataIndex === series.points.length - 1 ? 7 : 2,
            tension: 0.1
        };
    }).filter(Boolean);

    deviationChart.data.datasets = datasets;

    const bound = computeSymmetricBounds(datasets);
    deviationChart.options.scales.x.min = -bound;
    deviationChart.options.scales.x.max = bound;
    deviationChart.options.scales.y.min = -bound;
    deviationChart.options.scales.y.max = bound;
}

/* =-=-=-=-= INITIALISE CHART =-=-=-=-= */
function initChart() {
    const canvasElement = document.getElementById('deviationChart');

    deviationChart = new Chart(canvasElement, {
        type: 'scatter',
        plugins: [circularGridPlugin],
        data: {
            datasets: []
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { display: false },
                y: { display: false }
            },
            plugins: {
                legend: {
                    display: true,
                    position: 'top',
                    labels: {
                        boxWidth: 12,
                        color: '#2166a6'
                    }
                },
                tooltip: {
                    callbacks: {
                        title: (items) => items.length ? `Baseline: ${items[0].dataset.label}` : '',
                        label: (ctx) => {
                            const pt = ctx.raw;
                            return `Time: ${pt.time} | E: ${pt.x.toFixed(4)}m, N: ${pt.y.toFixed(4)}m, U: ${pt.u.toFixed(4)}m | ` +
                                `Dist: ${pt.distance.toFixed(4)}m | Q: ${pt.q}`;
                        }
                    }
                }
            }
        }
    });
}

// for the 'stop server' button in nav
async function stopServer() {
    if (confirm("Are you sure you want to stop the server?")) {
        await fetch('/api/shutdown', { method: 'POST' });
        document.body.innerHTML = "<h1>Server Has Been Shutdown</h1>";
    }
}

/* =-=-=-=-= LOAD HISTORICAL BASELINE DATA =-=-=-=-= */
async function loadInitialHistory() {
    try {
        const response = await fetch('/api/logs/history?max_points=3000');
        const json = await response.json();

        if (json.data && json.data.length > 0) {
            json.data.forEach(pt => addPointToState(pt));

            syncDatasetsFromBaselines();
            deviationChart.update();
            updateLiveReadout();
        }
    } catch (err) {
        console.error("Failed to load historical logs:", err);
    }
}

/* =-=-=-=-= SYSTEM STATUS BADGE =-=-=-=-= */
const STATUS_BADGE_IDS = {
    normal: 'status-badge-normal',
    warning: 'status-badge-warning',
    alarm: 'status-badge-alarm',
    error: 'status-badge-error'
};

function setSystemStatus(state, detailMessage) {
    Object.values(STATUS_BADGE_IDS).forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.remove('status-badge-active');
    });

    const activeId = STATUS_BADGE_IDS[state];
    const activeEl = activeId && document.getElementById(activeId);
    if (activeEl) activeEl.classList.add('status-badge-active');

    if (state === 'error') {
        const detailEl = document.getElementById('status-error-detail');
        if (detailEl) detailEl.textContent = detailMessage || 'Unknown error.';
    }
}

// Looks across each baseline's most recent point and shows the worst status
const STATUS_SEVERITY = { normal: 0, warning: 1, alarm: 2 };
function refreshWorstCaseStatus() {
    let worst = 'normal';
    latestByBaseline.forEach(pt => {
        if (STATUS_SEVERITY[pt.status] > STATUS_SEVERITY[worst]) {
            worst = pt.status;
        }
    });
    setSystemStatus(worst);
}

/* =-=-=-=-= FETCH LIVE UPDATES =-=-=-=-= */
let liveStreamSource = null;

function connectLiveStream() {
    liveStreamSource = new EventSource('/api/logs/stream');

    liveStreamSource.onopen = () => {
        // let refreshWorstCaseStatus() decide if NORMAL once real data starts arriving, so it doesn't flash green then alarm
    };

    liveStreamSource.onmessage = (event) => {
        const pt = JSON.parse(event.data);
        handleIncomingPoint(pt);
    };

    liveStreamSource.onerror = () => {
        const timestamp = new Date().toLocaleTimeString();
        setSystemStatus('error',
            `[${timestamp}] Lost connection to /api/logs/stream. Possible cause: ` +
            `server.py has been stopped or the launch.bat window was closed. `
        );
        console.warn("Live stream connection interrupted.");
    };
}

// normalises one server payload {time, baseline, e, n, u, distance_m, nominal_distance_m, q, ns, status}
// into the shape the chart/readout use, and stores it in both baselineSeries (history) and latestByBaseline (now).
function addPointToState(pt) {
    const series = ensureBaseline(pt.baseline);
    const point = {
        e: pt.e, n: pt.n, u: pt.u,
        distance: pt.distance_m,
        nominal: pt.nominal_distance_m,
        q: pt.q, ns: pt.ns,
        status: pt.status,
        time: pt.time
    };
    series.points.push(point);
    if (series.points.length > MAX_CLIENT_POINTS) {
        series.points.splice(0, series.points.length - MAX_CLIENT_POINTS);
    }
    latestByBaseline.set(pt.baseline, point);
}

function handleIncomingPoint(pt) {
    addPointToState(pt);
    syncDatasetsFromBaselines();
    deviationChart.update('none'); // Render update without completely redrawing the chart
    updateLiveReadout();
    refreshWorstCaseStatus();
}

/* =-=-=-=-= LIVE READOUT (Card 1) =-=-=-=-= */
function updateLiveReadout() {
    const statsElem = document.getElementById('baseline-live-readout');
    if (!statsElem || latestByBaseline.size === 0) return;

    const QUALITY_LABELS = { 1: 'FIX', 2: 'FLOAT', 3: 'SBAS', 4: 'DGPS', 5: 'SINGLE', 6: 'PPP', 0: 'NO FIX' };

    let lines = [];
    latestByBaseline.forEach((pt, name) => {
        const deviationMm = ((pt.distance - pt.nominal) * 1000).toFixed(1);
        lines.push(
            `<b>${name}:</b> ${pt.distance.toFixed(4)}m (target: ${pt.nominal.toFixed(2)}m, ` +
            `actual: ${deviationMm}mm) - ${QUALITY_LABELS[pt.q] || pt.q} - ${pt.time}`
        );
    });
    statsElem.innerHTML = lines.join('<br>');
}

/* =-=-=-=-= STATS TABLES (Card 3) =-=-=-=-= */
const DISTANCE_STATS_MAX_POINTS = 20000;
const DISTANCE_STATS_REFRESH_MS = 15000;

async function loadDistanceStats() {
    try {
        const response = await fetch(`/api/logs/distance_stats?max_points=${DISTANCE_STATS_MAX_POINTS}`);
        const json = await response.json();
        renderPairDistanceSummary(json);
        renderDistanceStatsTables(json);
    } catch (err) {
        console.error("Failed to load distance stats:", err);
    }
}

function formatMetres(value) {
    return value.toFixed(4);
}

// Fills in the short summary lines under the status badges
function renderPairDistanceSummary(json) {
    const container = document.getElementById('pairDistanceSummary');
    if (!container) return;

    const names = Object.keys(json.baselines).sort();
    if (names.length === 0) {
        container.innerHTML = '<p>Waiting for data from RTKNAVI...</p>';
        return;
    }

    container.innerHTML = names.map(name => {
        const b = json.baselines[name];
        return `<p>${name} = ${formatMetres(b.avg_distance_m)} m avg ` +
            `(target ${b.nominal_distance_m}m, min ${formatMetres(b.min_distance_m)}m / max ${formatMetres(b.max_distance_m)}m, ` +
            `${b.fix_rate_pct}% fixed)</p>`;
    }).join('');
}

// Builds the RTK Baseline Distance Statistics table
function renderDistanceStatsTables(json) {
    const container = document.getElementById('distanceStatsTables');
    if (!container) return;

    const names = Object.keys(json.baselines).sort();
    if (names.length === 0) {
        container.innerHTML = '<p>No logged data yet.</p>';
        return;
    }

    let table = '<table class="stats-table"><thead><tr>' +
        '<th>Baseline</th><th>Target (m)</th><th>Avg Dist (m)</th><th>Min Dist (m)</th>' +
        '<th>Max Dist (m)</th><th>Avg E/N/U (m)</th><th>Fix Rate</th><th>Samples</th>' +
        '</tr></thead><tbody>';

    names.forEach((name, idx) => {
        const b = json.baselines[name];
        const stripeClass = idx % 2 === 0 ? 'stats-stripe-a' : 'stats-stripe-b';
        table += `<tr class="${stripeClass}">`;
        table += `<td>${name}</td>`;
        table += `<td>${b.nominal_distance_m}</td>`;
        table += `<td>${formatMetres(b.avg_distance_m)}</td>`;
        table += `<td>${formatMetres(b.min_distance_m)}</td>`;
        table += `<td>${formatMetres(b.max_distance_m)}</td>`;
        table += `<td>${formatMetres(b.avg_e)} / ${formatMetres(b.avg_n)} / ${formatMetres(b.avg_u)}</td>`;
        table += `<td>${b.fix_rate_pct}%</td>`;
        table += `<td>${b.count}</td>`;
        table += '</tr>';
    });
    table += '</tbody></table>';

    const cappedNote = json.capped
        ? `<p class="stats-note">Showing the most recent ${json.points_considered.toLocaleString()} of ${json.total_recorded.toLocaleString()} logged points.</p>`
        : `<p class="stats-note">Based on all ${json.points_considered.toLocaleString()} logged points.</p>`;

    container.innerHTML = `<div class="stats-tables-row">${table}</div>${cappedNote}`;
}

window.stopServer = stopServer;
