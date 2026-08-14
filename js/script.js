// script.js

// for the deviation chart
let deviationChart = null;
let lastLiveIndex = 0;
// For storing points for computing live average position centre
let totalLat = 0;
let totalLon = 0;
let pointCount = 0;

/* =-=-=-=-= LOAD CHARTS =-=-=-=-= */
document.addEventListener("DOMContentLoaded", async () => {
    initChart();
    await loadInitialHistory();
    // poll for new live log updates every second - TODO NOTE FOR LILY should I make this a longer interval?
    setInterval(fetchLiveUpdates, 1000);
})

/* =-=-=-=-= DECORATE CHART TO BE CIRCULAR LIKE A RADER =-=-=-=-= */
const circularGridPlugin = {
    id: 'circularGrid',
    beforeDraw: (chart) => {
        const ctx = chart.ctx;
        const xAxis = chart.scales.x;
        const yAxis = chart.scales.y;

        // Find the mathematical center of the current view
        const centerX = xAxis.getPixelForValue((xAxis.max + xAxis.min) / 2);
        const centerY = yAxis.getPixelForValue((yAxis.max + yAxis.min) / 2);

        // Determine the maximum radius that fits in the canvas
        const maxRadius = Math.min(xAxis.right - centerX, centerY - yAxis.top);

        ctx.save();
        ctx.strokeStyle = 'rgba(150, 150, 150, 0.3)'; // Faint grey rings
        ctx.lineWidth = 1;

        // Draw 4 concentric circular rings
        for (let i = 1; i <= 4; i++) {
            ctx.beginPath();
            ctx.arc(centerX, centerY, maxRadius * (i / 4), 0, 2 * Math.PI);
            ctx.stroke();
        }

        // Draw vertical and horizontal crosshairs
        ctx.beginPath();
        ctx.moveTo(centerX, yAxis.top);
        ctx.lineTo(centerX, yAxis.bottom);
        ctx.moveTo(xAxis.left, centerY);
        ctx.lineTo(xAxis.right, centerY);
        ctx.stroke();

        ctx.restore();
    }
};

/* =-=-=-=-= INITIALISE CHART =-=-=-=-= */
function initChart() {
    const canvasElement = document.getElementById('deviationChart');

    // Progressive Line Animation Configuration
    const totalDuration = 2000;
    const delayBetweenPoints = totalDuration / 100;

    const animation = {
        x: {
            type: 'number',
            easing: 'linear',
            duration: delayBetweenPoints,
            from: NaN,
            delay(ctx) {
                if (ctx.type !== 'data' || ctx.xStarted) return 0;
                ctx.xStarted = true;
                return ctx.index * delayBetweenPoints;
            }
        },
        y: {
            type: 'number',
            easing: 'linear',
            duration: delayBetweenPoints,
            from: NaN,
            delay(ctx) {
                if (ctx.type !== 'data' || ctx.yStarted) return 0;
                ctx.yStarted = true;
                return ctx.index * delayBetweenPoints;
            }
        }
    };

    deviationChart = new Chart(canvasElement, {
        type: 'scatter',
        plugins: [circularGridPlugin], // Inject the custom circular grid
        data: {
            datasets: [
                {
                    label: 'Historical Path',
                    data: [],
                    showLine: true, // Connect the dots!
                    backgroundColor: 'rgba(54, 162, 235, 0.2)',
                    borderColor: 'rgba(54, 162, 235, 0.6)',
                    borderWidth: 2,
                    pointRadius: 2, // Smaller points to emphasize the line
                    tension: 0.1 // Slight curve to the connecting lines
                },
                {
                    label: 'Latest Position',
                    data: [],
                    backgroundColor: '#E30613',
                    pointRadius: 8,
                    pointHoverRadius: 10
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            aspectRatio: 1, // Forces the chart area to remain perfectly square for circular rings
            animation: animation, // Apply progressive animation
            scales: {
                x: {
                    display: false // Hide the default square grid lines and axis
                },
                y: {
                    display: false // Hide the default square grid lines and axis
                }
            },
            plugins: {
                tooltip: {
                    callbacks: {
                        label: function (ctx) {
                            const pt = ctx.raw;
                            return `Time: ${pt.time} | Lat: ${pt.y.toFixed(6)}, Lon: ${pt.x.toFixed(6)}`;
                        }
                    }
                }
            }
        }
    });
}

/* =-=-=-=-= LOAD HISTORICAL RECEIVER DATA =-=-=-=-= */
// hopefully should only run one on the initial load to minimise loading times
async function loadInitialHistory() {
    try {
        const response = await fetch('/api/logs/history');
        const json = await response.json();

        if (json.data && json.data.length > 0) {
            const historicalData = json.data.map(pt => {
                totalLat += pt.latitude;
                totalLon += pt.longitude;
                pointCount++;
                return { x: pt.longitude, y: pt.latitude, alt: pt.altitude, time: pt.time };
            });

            // append all historical data points to dataset 0
            deviationChart.data.datasets[0].data = historicalData;

            // Set latest position marker
            const latest = historicalData[historicalData.length - 1];
            deviationChart.data.datasets[1].data = [latest];

            deviationChart.update();
            updateMeanDisplay();
        }
    } catch (err) {
        console.error("Failed to load historical logs:", err);
    }
}

/* =-=-=-=-= FETCH LIVE UPDATES INCREMENTALLY =-=-=-=-= */
async function fetchLiveUpdates() {
    try {
        const response = await fetch(`/api/logs/live?since=${lastLiveIndex}`);
        const json = await response.json();

        if (json.data && json.data.length > 0) {
            lastLiveIndex = json.next_index;

            json.data.forEach(pt => {
                const newPoint = { x: pt.longitude, y: pt.latitude, alt: pt.altitude, time: pt.time };

                // Add to scatter point cloud chart
                deviationChart.data.datasets[0].data.push(newPoint);

                // Update mean calculations
                totalLat += pt.latitude;
                totalLon += pt.longitude;
                pointCount++;

                // Update latest position
                deviationChart.data.datasets[1].data = [newPoint];
            });

            // Render update without completely redrawing the chart
            deviationChart.update('none');
            updateMeanDisplay();
        }
    } catch (err) {
        console.error("Error fetching live stream updates:", err);
    }
}

/* =-=-=-=-= UPDATE MEAN LOCATION POINT =-=-=-=-= */
function updateMeanDisplay() {
    if (pointCount > 0) {
        const avgLat = (totalLat / pointCount).toFixed(6);
        const avgLon = (totalLon / pointCount).toFixed(6);
        document.getElementById('deviation-stats').innerText =
            `Center Mean (d): Lat ${avgLat}°, Lon ${avgLon}° | Total Sampled Points: ${pointCount}`;
    }
}

/* =-=-=-=-= SEND IP ADDRESSES =-=-=-=-= */
document.getElementById('gnss-form').addEventListener('submit', async function (e) {
    e.preventDefault();

    const ip1 = document.getElementById('ip1').value;
    const ip2 = document.getElementById('ip2').value;
    const ip3 = document.getElementById('ip3').value;
    const statusDisplay = document.getElementById('status-display');

    const D12 = document.getElementById('distance12-display');
    const D13 = document.getElementById('distance13-display');
    const D23 = document.getElementById('distance23-display');

    statusDisplay.innerText = "Connecting to streams..."
    D12.innerText = "..."
    D13.innerText = "..."
    D23.innerText = "..."

    // send data to fastapi server backend
    try {
        const response = await fetch('/api/start_tracking', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ ip_1: ip1, ip_2: ip2, ip_3: ip3 })
        });

        const data = await response.json();
        statusDisplay.innerText = "Tracking Active! Status: " + data.status;
        D12.innerText = "Distance between antennas -> 1&2: " + data.distance12;
        D13.innerText = "Distance between antennas -> 1&3: " + data.distance13;
        D23.innerText = "Distance between antennas -> 2&3: " + data.distance23;

    } catch (error) {
        statusDisplay.innerText = "Error connecting to server.";
        D12.innerText = ":("
        D13.innerText = ":("
        D23.innerText = ":("
    }
});
