// script.js

// for the deviation chart
let deviationChart = null;
// For storing points for computing live average position centre
let totalLat = 0;
let totalLon = 0;
let pointCount = 0;


// Map<receiverId, { points: [{x, y, time}], color: [r, g, b] }>
//Grouping data by receiver so mulptiple receivers can be shown at once in different colours
const receiverSeries = new Map();

const COLOR_PALETTE = [
    [227, 6, 19],    // red
    [54, 162, 235],  // blue
    [46, 184, 92],   // green
    [255, 159, 64],  // orange
    [153, 102, 255], // purple
    [0, 188, 212],   // cyan
];
let nextColorIndex = 0;

// Safety cap for load times - oldest points are dropped once a receiver passes this
// The full history always stays in the CSV on the server
const MAX_CLIENT_POINTS = 5000;

/* =-=-=-=-= LOAD CHARTS =-=-=-=-= */
document.addEventListener("DOMContentLoaded", async () => {
    initChart();
    await loadInitialHistory(); // one-off fetch of everything recorded before this page loaded
    connectLiveStream(); // live push connection for everything after that

    loadDistanceStats(); //variance table
    setInterval(loadDistanceStats, DISTANCE_STATS_REFRESH_MS);
})

/* =-=-=-=-= DECORATE CHART =-=-=-=-= */
const circularGridPlugin = {
    id: 'circularGrid',
    beforeDraw: (chart) => {
        const ctx = chart.ctx;
        const xAxis = chart.scales.x;
        const yAxis = chart.scales.y;

        // Find the mathematical centre of the current view
        const centreX = xAxis.getPixelForValue((xAxis.max + xAxis.min) / 2);
        const centreY = yAxis.getPixelForValue((yAxis.max + yAxis.min) / 2);

        // Determine the maximum radius that fits in the canvas
        const maxRadius = Math.min(xAxis.right - centreX, centreY - yAxis.top);
        // meters
        const maxMetres = (xAxis.max - xAxis.min) / 2;

        ctx.save();
        ctx.strokeStyle = 'rgba(150, 150, 150, 0.3)'; // Faint grey rings        	
        ctx.fillStyle = 'rgba(166, 35, 33, 0.9)'
        ctx.font = '11px Arial';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.lineWidth = 1;

        // Draw 4 circular rings
        for (let i = 1; i <= 4; i++) {
            const r = maxRadius * (i / 4);
            ctx.beginPath();
            ctx.arc(centreX, centreY, r, 0, 2 * Math.PI);
            ctx.stroke();
            const ringMetres = (maxMetres * (i / 4)).toFixed(1);
            // label in the top right of each ring
            ctx.fillText(`${ringMetres}m`, centreX + r * Math.SQRT1_2 + 4, centreY - r * Math.SQRT1_2);
        }

        // Draw vertical and horizontal crosshairs
        ctx.strokeStyle = 'rgba(0, 0, 0, 0.3)'; //crosshairs black
        ctx.beginPath();
        ctx.moveTo(centreX, yAxis.top);
        ctx.lineTo(centreX, yAxis.bottom);
        ctx.moveTo(xAxis.left, centreY);
        ctx.lineTo(xAxis.right, centreY);
        ctx.stroke();

        // Compass labels: +y (up) = North, +x (right) = East
        ctx.fillStyle = 'rgba(166, 35, 33, 0.9)'; // compass points dark red
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
// Fades a colour from light (old points) to full opacity (newest points)
function fadeColor([r, g, b], index, total) {
    if (total <= 1) return `rgba(${r}, ${g}, ${b}, 1)`;
    const minOpacity = 0.25; //opacity lily TODO it's here since I keep losing it
    const t = index / (total - 1); // 0 = oldest point, 1 = newest point
    const opacity = minOpacity + (1 - minOpacity) * t;
    return `rgba(${r}, ${g}, ${b}, ${opacity.toFixed(3)})`;
}

// Ensures a receiver has an entry in receiverSeries, assigning it the next palette colour the first time it's seen.
function ensureReceiver(receiverId) {
    if (!receiverSeries.has(receiverId)) {
        const color = COLOR_PALETTE[nextColorIndex % COLOR_PALETTE.length];
        nextColorIndex++;
        receiverSeries.set(receiverId, { points: [], color });
    }
    return receiverSeries.get(receiverId);
}

/* =-=-=-=-= LAT/LON -> METRES CONVERSION CONSTANTS =-=-=-=-= */
const METRES_PER_DEG_LAT = 111120;
const METRES_PER_DEG_LON = 65315; // Assumes ~54°N latitude (make dynamic?)

function computeSymmetricBounds(datasets) {
    // floor of 1m that grows automatically if a point deviates further LILY TODO I MESSED WITH THIS IT USED TO BE 6
    let maxAbs = 1;
    datasets.forEach(ds => {
        ds.data.forEach(pt => {
            maxAbs = Math.max(maxAbs, Math.abs(pt.x || 0), Math.abs(pt.y || 0));
        });
    });
    return maxAbs * 1.15;
}

// Shared reference point for all receivers so the chart doesn't jump around as new points stream in and each receiver shares the same origin
let globalCenter = null;

function establishGlobalCenterIfNeeded() {
    if (globalCenter !== null) return;

    let sumLat = 0, sumLon = 0, count = 0;
    receiverSeries.forEach(series => {
        series.points.forEach(p => {
            sumLat += p.lat;
            sumLon += p.lon;
            count++;
        });
    });

    if (count > 0) {
        globalCenter = { lat: sumLat / count, lon: sumLon / count };
    }
}

// Rebuilds chart.data.datasets from receiverSeries
// one dataset per receiver, with older points faded out and the newest point drawn larger
function syncDatasetsFromReceivers() {
    establishGlobalCenterIfNeeded();
    if (!globalCenter) return; // no data loaded yet
    const receiversArray = Array.from(receiverSeries.entries());

    const datasets = receiversArray.map(([receiverId, series]) => {
        if (series.points.length === 0) return null;

        // Convert points relative to the shared global centre so real distances are shown
        const mappedData = series.points.map(pt => ({
            x: (pt.lon - globalCenter.lon) * METRES_PER_DEG_LON,
            y: (pt.lat - globalCenter.lat) * METRES_PER_DEG_LAT,
            lat: pt.lat,
            lon: pt.lon,
            time: pt.time
        }));

        return {
            label: receiverId,
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

    // Triangle marker to represent mean receiver position 
    // (keeping it in a separate array and drawn last so it doesn't go under the other plots)
    const averageMarkerDatasets = receiversArray.map(([receiverId, series]) => {
        if (series.points.length === 0) return null;

        const avgLat = series.points.reduce((acc, p) => acc + p.lat, 0) / series.points.length;
        const avgLon = series.points.reduce((acc, p) => acc + p.lon, 0) / series.points.length;

        return {
            label: `${receiverId} (avg)`,
            isAverageMarker: true,
            showLine: false,
            data: [{
                x: (avgLon - globalCenter.lon) * METRES_PER_DEG_LON,
                y: (avgLat - globalCenter.lat) * METRES_PER_DEG_LAT,
                lat: avgLat,
                lon: avgLon
            }],
            pointStyle: 'triangle',
            rotation: 0,
            pointRadius: 8,
            pointHoverRadius: 10,
            backgroundColor: `rgba(${series.color.join(', ')}, 1)`, // filled with the receiver's own colour
            borderColor: '#000000', // black outline
            borderWidth: 2,
            order: -1 // lower order draws last so it always sits above the other datasets
        };
    }).filter(Boolean);

    deviationChart.data.datasets = [...datasets, ...averageMarkerDatasets];

    // Calculate auto-bounds based on mapped x, y coordinates
    const bound = computeSymmetricBounds([...datasets, ...averageMarkerDatasets]);
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
        plugins: [circularGridPlugin], // Inject custom circular grid
        data: {
            datasets: []
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: {
                    display: false // Hide the default square grid lines and axis
                },
                y: {
                    display: false
                }
            },
            plugins: {
                legend: {
                    display: true,
                    position: 'top',
                    labels: {
                        boxWidth: 12,
                        color: '#2166a6',
                        // keep average triangles out of legend
                        filter: (legendItem, chartData) => !chartData.datasets[legendItem.datasetIndex]?.isAverageMarker
                    }
                },
                tooltip: {
                    callbacks: {
                        title: function (items) {
                            if (!items.length) return '';
                            const ds = items[0].dataset;
                            return ds.isAverageMarker
                                ? `${ds.label.replace(' (avg)', '')} \u2014 Average Position`
                                : `Receiver: ${ds.label}`;
                        },
                        label: function (ctx) {
                            const pt = ctx.raw;
                            return ctx.dataset.isAverageMarker
                                ? `Mean Lat: ${pt.lat.toFixed(6)}\u00b0, Mean Lon: ${pt.lon.toFixed(6)}\u00b0`
                                : `Time: ${pt.time} | Lat: ${pt.y.toFixed(6)}, Lon: ${pt.x.toFixed(6)}`;
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

/* =-=-=-=-= LOAD HISTORICAL RECEIVER DATA =-=-=-=-= */
// runs once on ititial load but now also caps how many rows the server sends back and 
// thins the data if there's too much history built up

async function loadInitialHistory() {
    try {
        const response = await fetch('/api/logs/history?max_points=3000'); // todo Lily I changed this max points plots 
        const json = await response.json();

        if (json.data && json.data.length > 0) {
            json.data.forEach(pt => {
                totalLat += pt.latitude;
                totalLon += pt.longitude;
                pointCount++;

                const series = ensureReceiver(pt.receiver);
                // Store raw lat and lon for local mean calculations
                series.points.push({ lat: pt.latitude, lon: pt.longitude, time: pt.time });
            });

            syncDatasetsFromReceivers();
            deviationChart.update();
            updateMeanDisplay();
        }
    } catch (err) {
        console.error("Failed to load historical logs:", err);
    }
}

/* =-=-=-=-= SYSTEM STATUS BADGE =-=-=-=-= */
// makes it so only one of the status badges shows at any one time by 
// swapping status-badge-active onto whichever one is applicable
// normal is default

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

/* =-=-=-=-= FETCH LIVE UPDATES =-=-=-=-= */
// The server pushes each new point down a single persistent connection 
// the moment it's parsed, so the page is never reloading
let liveStreamSource = null;

function connectLiveStream() {
    liveStreamSource = new EventSource('/api/logs/stream');

    // when connection is established or reopened, clears previous ERROR badge
    liveStreamSource.onopen = () => {
        setSystemStatus('normal');
    };

    liveStreamSource.onmessage = (event) => {
        const pt = JSON.parse(event.data);
        handleIncomingPoint(pt);
    };

    liveStreamSource.onerror = () => {
        // EventSource retries the connection automatically
        const timestamp = new Date().toLocaleTimeString();
        setSystemStatus('error',
            `[${timestamp}] Lost connection to /api/logs/stream. Possible cause: ` +
            `server.py has been stopped or the launch.bat window was closed. ` +
            `Retrying automatically in the background...`
        );
        console.warn("Live stream connection interrupted - browser will auto-reconnect.");
    };
}

function handleIncomingPoint(pt) {
    // Update mean calculations
    totalLat += pt.latitude;
    totalLon += pt.longitude;
    pointCount++;

    const series = ensureReceiver(pt.receiver);
    // Store raw lat and lon for local mean calculations
    series.points.push({ lat: pt.latitude, lon: pt.longitude, time: pt.time });

    if (series.points.length > MAX_CLIENT_POINTS) {
        series.points.splice(0, series.points.length - MAX_CLIENT_POINTS);
    }

    syncDatasetsFromReceivers();
    deviationChart.update('none'); // Render update without completely redrawing the chart
    updateMeanDisplay();
}

/* =-=-=-=-= UPDATE MEAN LOCATION =-=-=-=-= */
function updateMeanDisplay() {
    const statsElem = document.getElementById('deviation-stats');
    if (!statsElem || receiverSeries.size === 0) return;

    let outputLines = [];

    receiverSeries.forEach((series, receiverId) => {
        if (series.points.length === 0) return;
        const avgLat = (series.points.reduce((acc, p) => acc + p.lat, 0) / series.points.length).toFixed(6);
        const avgLon = (series.points.reduce((acc, p) => acc + p.lon, 0) / series.points.length).toFixed(6);
        outputLines.push(`<b>${receiverId} Mean:</b> Lat ${avgLat}°, Lon ${avgLon}° (${series.points.length} pts)`);
    });

    statsElem.innerHTML = outputLines.join('<br>');
}

/* =-=-=-=-= VARIANCE TABLE =-=-=-=-= */
// see /api/logs/distance_stats on the server
const DISTANCE_STATS_MAX_POINTS = 20000; // longer history cap independent of the live map's cap
const DISTANCE_STATS_REFRESH_MS = 15000; // refreshes less often

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

function formatDeg(value) {
    return value.toFixed(8);
}

function formatMetres(value) {
    return value.toFixed(3);
}

// Fills in summary lines
function renderPairDistanceSummary(json) {
    const container = document.getElementById('pairDistanceSummary');
    if (!container) return;

    const pairKeys = Object.keys(json.pairs).sort();
    if (pairKeys.length === 0) {
        container.innerHTML = '<p>Waiting for data from two or more receivers...</p>';
        return;
    }

    container.innerHTML = pairKeys.map(key => {
        const pair = json.pairs[key];
        const [a, b] = key.split('-');
        return `<p>${a} \u2194 ${b} = ${formatMetres(pair.average.distance_m)} m avg ` +
            `(min ${formatMetres(pair.min_distance.distance_m)} m / max ${formatMetres(pair.max_distance.distance_m)} m)</p>`;
    }).join('');
}

// Builds the two full stats tables (per-port lat/lon, per-pair distance)
function renderDistanceStatsTables(json) {
    const container = document.getElementById('distanceStatsTables');
    if (!container) return;

    const receiverIds = Object.keys(json.receivers).sort(
        (a, b) => json.receivers[a].port.localeCompare(json.receivers[b].port)
    );

    if (receiverIds.length === 0) {
        container.innerHTML = '<p>No logged data yet.</p>';
        return;
    }

    // Table 1: per-port lat/lon mean/upper/lower
    let table1 = '<table class="stats-table"><thead><tr><th>Metric</th><th>Column</th>';
    receiverIds.forEach(id => {
        table1 += `<th>Port ${json.receivers[id].port}</th>`;
    });
    table1 += '</tr></thead><tbody>';

    const positionGroups = [
        { label: 'Mean', fields: [['Lat', 'avg_lat'], ['Lon', 'avg_lon']] },
        { label: 'Upper (Max)', fields: [['Lat', 'max_lat'], ['Lon', 'max_lon']] },
        { label: 'Lower (Min)', fields: [['Lat', 'min_lat'], ['Lon', 'min_lon']] },
    ];

    positionGroups.forEach((group, groupIdx) => {
        const stripeClass = groupIdx % 2 === 0 ? 'stats-stripe-a' : 'stats-stripe-b';
        group.fields.forEach(([label, field], idx) => {
            table1 += `<tr class="${stripeClass}">`;
            if (idx === 0) {
                table1 += `<td class="stats-rowgroup" rowspan="2">${group.label}</td>`;
            }
            table1 += `<td>${label}</td>`;
            receiverIds.forEach(id => {
                table1 += `<td>${formatDeg(json.receivers[id][field])}</td>`;
            });
            table1 += '</tr>';
        });
    });
    table1 += '</tbody></table>';

    // Table 2: pairs distance stats
    const pairKeys = Object.keys(json.pairs).sort();
    let table2 = '<table class="stats-table"><thead><tr><th>Metric</th><th>Port Pair</th>' +
        '<th>Lat Diff (deg)</th><th>Lon Diff (deg)</th><th>Distance (Metres)</th></tr></thead><tbody>';

    const distanceGroups = [
        { label: 'Average', key: 'average' },
        { label: 'Largest Distance (Max)', key: 'max_distance' },
        { label: 'Smallest Distance (Min)', key: 'min_distance' },
    ];

    distanceGroups.forEach((metric, groupIdx) => {
        const stripeClass = groupIdx % 2 === 0 ? 'stats-stripe-a' : 'stats-stripe-b';
        pairKeys.forEach((pairKey, idx) => {
            const pair = json.pairs[pairKey][metric.key];
            table2 += `<tr class="${stripeClass}">`;
            if (idx === 0) {
                table2 += `<td class="stats-rowgroup" rowspan="${pairKeys.length}">${metric.label}</td>`;
            }
            table2 += `<td>${pairKey}</td>`;
            table2 += `<td>${pair.lat_diff.toExponential(5)}</td>`;
            table2 += `<td>${pair.lon_diff.toExponential(5)}</td>`;
            table2 += `<td>${formatMetres(pair.distance_m)}</td>`;
            table2 += '</tr>';
        });
    });
    table2 += '</tbody></table>';

    const cappedNote = json.capped
        ? `<p class="stats-note">Showing the most recent ${json.points_considered.toLocaleString()} of ${json.total_recorded.toLocaleString()} logged points.</p>`
        : `<p class="stats-note">Based on all ${json.points_considered.toLocaleString()} logged points.</p>`;

    container.innerHTML = `<div class="stats-tables-row">${table1}${table2}</div>${cappedNote}`;
}

// for the 'restart server' button to work
async function restartServer() {
    if (!confirm("Are you sure you want to restart the server?")) return;

    try {
        await fetch('/api/restart', { method: 'POST' });
        document.body.innerHTML = "<h1>Server is restarting... Please wait.</h1>";

        // Wait 3 seconds, then ping server every 2 seconds until it responds
        setTimeout(() => {
            const checkServer = setInterval(async () => {
                try {
                    const response = await fetch('/', { method: 'HEAD' });
                    if (response.ok) {
                        clearInterval(checkServer);
                        location.reload();
                    }
                } catch (err) {
                    // Server is still starting up...
                }
            }, 2000);
        }, 3000);
    } catch (err) {
        alert("Failed to send restart signal to server.");
    }
}

window.stopServer = stopServer;
window.restartServer = restartServer; 