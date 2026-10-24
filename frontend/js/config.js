/* Tunables that used to be scattered as literals across the UI code. */

// A node is considered offline this many seconds after its last report.
export const OFFLINE_AFTER = 15;

// Electricity tariff behind the running cost (AGL "Powering On", time of use,
// bill of 19 Sep - 18 Oct 2026). Copy the per-kWh prices as printed: the bill
// lists them before GST, which is added on top. The daily supply charge is
// left out on purpose, since it is owed whether or not the lab runs.
export const TARIFF = {
    peak: 0.4979,                             // $/kWh, during peak hours in peak months
    offPeak: 0.1988,                          // $/kWh, every other hour
    peakHours: [15, 21],                      // 3pm up to 9pm, local time
    peakMonths: [11, 12, 1, 2, 3, 6, 7, 8],   // Nov-Mar and Jun-Aug
    gst: 0.10,
};

/**
 * $/kWh, GST included, for a draw that runs around the clock this month:
 * off-peak all day outside the peak months, a peak/off-peak blend inside them.
 */
export function effectiveRate(date = new Date()) {
    const { peak, offPeak, peakHours: [from, to], peakMonths, gst } = TARIFF;
    const peakShare = peakMonths.includes(date.getMonth() + 1) ? (to - from) / 24 : 0;
    return (peak * peakShare + offPeak * (1 - peakShare)) * (1 + gst);
}

/** Watts -> dollars over a 30-day month at this month's rate. */
export const monthlyCost = (watts) => (watts / 1000) * 24 * 30 * effectiveRate();

// Alert thresholds. Each has a separate recovery point (hysteresis) so a
// metric hovering on the line does not flap between alarm and clear.
export const THRESHOLDS = {
    cpu:     { trip: 85, clear: 75, label: 'CPU load' },
    temp:    { trip: 80, clear: 72, label: 'CPU temperature' },
    gpuUtil: { trip: 85, clear: 75, label: 'GPU load' },
    gpuTemp: { trip: 80, clear: 72, label: 'GPU temperature' },
};

// Points at which a meter shifts from neutral to amber to red.
export const LOAD_WARN = 75;
export const LOAD_CRIT = 90;

export const TEMP_WARN = 65;
export const TEMP_CRIT = 80;

export const LATENCY_WARN = 60;
export const LATENCY_CRIT = 150;

// Sparkline ring-buffer length.
export const HISTORY_POINTS = 40;

// Minimum gap between alarm chimes.
export const CHIME_THROTTLE_MS = 20000;
