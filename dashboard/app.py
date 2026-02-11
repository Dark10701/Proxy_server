from flask import Flask, render_template
import csv
import os
from collections import Counter, defaultdict
from datetime import datetime
import statistics

app = Flask(__name__)

# Helper to locate metrics file
def get_metrics_path():
    # Assuming app is run from dashboard/ or root
    # Try finding logs/metrics.csv relative to app.py
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Check parent dir (if running from dashboard/)
    path = os.path.join(base_dir, '..', 'logs', 'metrics.csv')
    if os.path.exists(path):
        return path
    # Check proxy_server dir (common structure)
    path = os.path.join(base_dir, '..', 'proxy_server', 'logs', 'metrics.csv')
    if os.path.exists(path):
        return path
    # Check current dir (if logs is inside dashboard, unlikely)
    path = os.path.join(base_dir, 'logs', 'metrics.csv')
    if os.path.exists(path):
        return path
    return None

def parse_metrics():
    path = get_metrics_path()
    if not path or not os.path.exists(path):
        return None

    data = []
    try:
        with open(path, 'r', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                data.append(row)
    except Exception as e:
        print(f"Error reading metrics: {e}")
        return None
    return data

def calculate_stats(data):
    if not data:
        return {
            'total_requests': 0,
            'blocked_requests': 0,
            'avg_latency': 0,
            'total_bandwidth': 0,
            'unique_clients': 0,
            'top_domains_labels': [],
            'top_domains_data': [],
            'requests_time_labels': [],
            'requests_time_data': [],
            'latency_time_data': [],
            'bw_domains_labels': [],
            'bw_domains_data': []
        }

    total_requests = len(data)
    # Blocked requests are not logged in metrics.csv currently
    blocked_requests = 0

    latencies = []
    bandwidth = 0
    clients = set()
    domains = []

    # Time series data
    req_per_min = defaultdict(int)
    latency_per_min = defaultdict(list)
    bandwidth_per_domain = defaultdict(int)

    for row in data:
        # 1. Timestamp & Request Count
        minute_key = None
        ts_str = row.get('timestamp', '')
        if ts_str:
            try:
                # Try format: "DD-MM-YYYY  HH:MM:SS" (two spaces)
                dt = datetime.strptime(ts_str, "%d-%m-%Y  %H:%M:%S")
            except ValueError:
                try:
                    # Fallback to standard: "YYYY-MM-DD HH:MM:SS"
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    dt = None

            if dt:
                minute_key = dt.strftime("%H:%M")
                req_per_min[minute_key] += 1

        # 2. Latency
        try:
            lat_str = row.get('latency_ms', '')
            if lat_str:
                lat = int(lat_str)
                latencies.append(lat)
                if minute_key:
                    latency_per_min[minute_key].append(lat)
        except ValueError:
            pass

        # 3. Bandwidth
        try:
            bw_str = row.get('response_bytes', '')
            if bw_str:
                b = int(bw_str)
                bandwidth += b

                # Bandwidth per domain
                h = row.get('host', '')
                if h:
                    bandwidth_per_domain[h] += b
        except ValueError:
            pass

        # 4. Client
        client = row.get('client_ip', '')
        if client:
            clients.add(client)

        # 5. Host
        host = row.get('host', '')
        if host:
            domains.append(host)

    avg_latency = statistics.mean(latencies) if latencies else 0

    # Top domains
    domain_counts = Counter(domains)
    top_domains = domain_counts.most_common(5)

    # Format time series for charts
    sorted_mins = sorted(req_per_min.keys())
    requests_time_labels = sorted_mins
    requests_time_data = [req_per_min[k] for k in sorted_mins]

    latency_time_data = []
    for k in sorted_mins:
        lats = latency_per_min[k]
        latency_time_data.append(statistics.mean(lats) if lats else 0)

    # Top 5 bandwidth domains
    top_bw_domains = sorted(bandwidth_per_domain.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        'total_requests': total_requests,
        'blocked_requests': blocked_requests,
        'avg_latency': round(avg_latency, 2),
        'total_bandwidth': round(bandwidth / (1024*1024), 2), # MB
        'unique_clients': len(clients),
        'top_domains_labels': [d[0] for d in top_domains],
        'top_domains_data': [d[1] for d in top_domains],
        'requests_time_labels': requests_time_labels,
        'requests_time_data': requests_time_data,
        'latency_time_data': latency_time_data,
        'bw_domains_labels': [d[0] for d in top_bw_domains],
        'bw_domains_data': [round(d[1]/(1024*1024), 2) for d in top_bw_domains]
    }

@app.route('/')
def index():
    data = parse_metrics()
    stats = calculate_stats(data)
    return render_template('index.html', stats=stats)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
