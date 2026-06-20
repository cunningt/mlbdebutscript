#!/usr/bin/env python3
"""
MLB Debuts Tracker
Scrapes ESPN for recent MLB debuts, fetches minor league stats from Baseball Reference,
researches prospect rankings, and generates an HTML email summary.
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from openai import OpenAI
from ddgs import DDGS
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
import time
import mysql.connector


DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "database": "bingle",
    "user": "root",
    "password": "iverson3"
}


def check_bsl_ownership(player_name):
    """Check if a player is owned in the BSL league."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # Parse player name - handle "First Last" format
        parts = player_name.strip().split()
        if len(parts) >= 2:
            first_name = parts[0]
            last_name = " ".join(parts[1:])
            # Search for "Last, First" format used in database
            search_pattern = f"%{last_name}, {first_name}%"
        else:
            search_pattern = f"%{player_name}%"

        cursor.execute(
            "SELECT mlbid, name FROM bslownership WHERE name LIKE %s",
            (search_pattern,)
        )
        rows = cursor.fetchall()

        cursor.close()
        conn.close()

        if rows:
            return {"owned": True, "mlbid": rows[0][0], "owner_entry": rows[0][1]}
        return {"owned": False, "mlbid": None, "owner_entry": None}

    except Exception as e:
        print(f"    Error checking BSL ownership: {e}")
        return {"owned": None, "mlbid": None, "owner_entry": None}


def load_email_config():
    """Load email configuration from email.properties file."""
    config = {}
    config_path = Path(__file__).parent / "email.properties"

    with open(config_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

    return config


def send_email(subject, html_body, text_body):
    """Send HTML email using Gmail SMTP."""
    config = load_email_config()

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{config['smtp.from.name']} <{config['smtp.username']}>"
    msg["To"] = config["recipients"]
    msg["Subject"] = f"{config['subject.prefix']} {subject}"

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    recipients = [r.strip() for r in config["recipients"].split(",")]

    try:
        with smtplib.SMTP(config["smtp.host"], int(config["smtp.port"])) as server:
            server.starttls()
            server.login(config["smtp.username"], config["smtp.password"])
            server.sendmail(config["smtp.username"], recipients, msg.as_string())
        print(f"Email sent successfully to: {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def fetch_espn_debuts():
    """Fetch and parse the ESPN MLB debuts page."""
    url = "https://www.espn.com/mlb/debuts"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    return soup


def parse_debuts(soup, days_back=2):
    """Extract debuts from the last N days."""
    debuts = []
    today = datetime.now().date()
    cutoff_date = today - timedelta(days=days_back)
    current_year = datetime.now().year

    table = soup.find("table")
    if not table:
        print("No table found on page")
        return debuts

    rows = table.find_all("tr")

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 4:
            continue

        cell_texts = [cell.get_text(strip=True) for cell in cells]

        if cell_texts[0] == "DATE":
            continue

        date_str = cell_texts[0]
        player_team = cell_texts[1]
        position = cell_texts[2] if len(cell_texts) > 2 else "Unknown"
        age = cell_texts[3] if len(cell_texts) > 3 else "Unknown"
        debut_stats = cell_texts[4] if len(cell_texts) > 4 else ""

        try:
            month, day = map(int, date_str.split("/"))
            debut_date = datetime(current_year, month, day).date()
        except (ValueError, AttributeError):
            continue

        if debut_date < cutoff_date:
            continue

        if ", " in player_team:
            name, team = player_team.rsplit(", ", 1)
        else:
            name = player_team
            team = "Unknown"

        debuts.append({
            "name": name,
            "team": team,
            "position": position,
            "age": age,
            "debut_date": debut_date,
            "debut_stats": debut_stats
        })

    return debuts


def find_bref_player_id(player_name):
    """Search Baseball Reference for a player and return their register ID."""
    search_url = f"https://www.baseball-reference.com/search/search.fcgi?search={player_name.replace(' ', '+')}"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    try:
        response = requests.get(search_url, headers=headers, allow_redirects=True, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")

        if "/players/" in response.url:
            links = soup.find_all("a", href=True)
            for link in links:
                href = link.get("href", "")
                if "/register/player.fcgi?id=" in href:
                    return href.split("id=")[1]

        elif "/register/player.fcgi" in response.url:
            return response.url.split("id=")[1]

        elif "/search/" in response.url:
            search_results = soup.find_all("div", class_="search-item")
            if not search_results:
                search_results = soup.find_all("div", class_="search-item-url")
            for item in search_results:
                link = item.find("a", href=True)
                if link:
                    href = link.get("href", "")
                    if "/register/player.fcgi?id=" in href:
                        return href.split("id=")[1]
                    elif "/players/" in href:
                        player_page = requests.get(f"https://www.baseball-reference.com{href}", headers=headers, timeout=10)
                        player_soup = BeautifulSoup(player_page.text, "html.parser")
                        for plink in player_soup.find_all("a", href=True):
                            phref = plink.get("href", "")
                            if "/register/player.fcgi?id=" in phref:
                                return phref.split("id=")[1]
                        break

    except Exception as e:
        print(f"    Error searching for {player_name}: {e}")

    return None


def get_minor_league_stats(player_id):
    """Fetch minor league stats from Baseball Reference register page."""
    if not player_id:
        return None

    url = f"https://www.baseball-reference.com/register/player.fcgi?id={player_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")

        batting_table = soup.find("table", {"id": "standard_batting"})
        pitching_table = soup.find("table", {"id": "standard_pitching"})

        stats = {"batting": [], "pitching": [], "career_batting": None, "career_pitching": None, "positions_2026": []}

        if batting_table:
            tbody = batting_table.find("tbody")
            if tbody:
                rows = tbody.find_all("tr", class_=re.compile(r"minors|majors"))
                for row in rows:
                    cells = row.find_all(["th", "td"])
                    data = {cell.get("data-stat"): cell.get_text(strip=True) for cell in cells}
                    if data.get("level") != "Maj":
                        stats["batting"].append(data)

            tfoot = batting_table.find("tfoot")
            if tfoot:
                for row in tfoot.find_all("tr"):
                    row_class = row.get("class", [])
                    th = row.find("th")
                    label = th.get_text(strip=True) if th else ""
                    if "minors" in row_class and "seasons" in label.lower():
                        cells = row.find_all(["th", "td"])
                        stats["career_batting"] = {cell.get("data-stat"): cell.get_text(strip=True) for cell in cells}
                        break

        if pitching_table:
            tbody = pitching_table.find("tbody")
            if tbody:
                rows = tbody.find_all("tr", class_=re.compile(r"minors|majors"))
                for row in rows:
                    cells = row.find_all(["th", "td"])
                    data = {cell.get("data-stat"): cell.get_text(strip=True) for cell in cells}
                    if data.get("level") != "Maj":
                        stats["pitching"].append(data)

            tfoot = pitching_table.find("tfoot")
            if tfoot:
                for row in tfoot.find_all("tr"):
                    row_class = row.get("class", [])
                    th = row.find("th")
                    label = th.get_text(strip=True) if th else ""
                    if "minors" in row_class and "seasons" in label.lower():
                        cells = row.find_all(["th", "td"])
                        stats["career_pitching"] = {cell.get("data-stat"): cell.get_text(strip=True) for cell in cells}
                        break

        # Extract 2026 fielding positions from HTML comments
        current_year = str(datetime.now().year)
        html_comments = re.findall(r'<!--(.+?)-->', response.text, re.DOTALL)
        for comment in html_comments:
            if 'standard_fielding' in comment:
                comment_soup = BeautifulSoup(comment, 'html.parser')
                fielding_table = comment_soup.find('table', {'id': 'standard_fielding'})
                if fielding_table:
                    tbody = fielding_table.find('tbody')
                    if tbody:
                        for row in tbody.find_all('tr'):
                            cells = row.find_all(['th', 'td'])
                            data = {cell.get('data-stat'): cell.get_text(strip=True) for cell in cells}
                            year = data.get('year_ID', '')
                            level = data.get('level', '')
                            pos = data.get('POS', '')
                            games = data.get('G', '')
                            # Only include 2026 minor league positions
                            if current_year in year and level != 'Maj' and pos and games:
                                stats["positions_2026"].append({
                                    "pos": pos,
                                    "games": int(games) if games.isdigit() else 0,
                                    "level": level
                                })
                break

        # Consolidate positions by summing games across levels
        pos_totals = {}
        for p in stats["positions_2026"]:
            pos = p["pos"]
            if pos in pos_totals:
                pos_totals[pos] += p["games"]
            else:
                pos_totals[pos] = p["games"]

        stats["positions_2026"] = [
            {"pos": pos, "games": games}
            for pos, games in sorted(pos_totals.items(), key=lambda x: x[1], reverse=True)
        ]

        return stats

    except Exception as e:
        print(f"    Error fetching stats: {e}")
        return None


def search_prospect_info(player_name, team):
    """Search for prospect rankings from MLB Pipeline, Fangraphs, Baseball America."""
    search_queries = [
        f'"{player_name}" MLB Pipeline prospect ranking',
        f'"{player_name}" Fangraphs prospect scouting report',
        f'"{player_name}" Baseball America prospect',
    ]

    all_results = []

    with DDGS() as ddgs:
        for query in search_queries:
            try:
                results = list(ddgs.text(query, max_results=2))
                for r in results:
                    source = identify_source(r.get("href", ""))
                    all_results.append({
                        "source": source,
                        "title": r.get("title", ""),
                        "body": r.get("body", ""),
                        "url": r.get("href", "")
                    })
            except Exception as e:
                print(f"    Search error: {e}")
            time.sleep(0.5)

    return all_results


def identify_source(url):
    """Identify the source based on URL."""
    url = url.lower()
    if "mlb.com/prospects" in url or "mlb.com/pipeline" in url:
        return "MLB Pipeline"
    elif "fangraphs.com" in url:
        return "Fangraphs"
    elif "baseballamerica.com" in url:
        return "Baseball America"
    elif "mlb.com" in url:
        return "MLB.com"
    else:
        return "Other"


def format_batting_stats_table(stats):
    """Format batting stats as HTML table rows."""
    if not stats:
        return ""

    rows = []
    for s in stats[-4:]:
        rows.append(f"""
            <tr>
                <td>{s.get('year_ID', '')}</td>
                <td>{s.get('team_ID', '')}</td>
                <td>{s.get('level', '')}</td>
                <td>{s.get('G', '')}</td>
                <td>{s.get('PA', '')}</td>
                <td>{s.get('batting_avg', '')}</td>
                <td>{s.get('HR', '')}</td>
                <td>{s.get('RBI', '')}</td>
                <td>{s.get('SB', '')}</td>
                <td>{s.get('onbase_plus_slugging', '')}</td>
            </tr>
        """)
    return "\n".join(rows)


def format_pitching_stats_table(stats):
    """Format pitching stats as HTML table rows."""
    if not stats:
        return ""

    rows = []
    for s in stats[-4:]:
        rows.append(f"""
            <tr>
                <td>{s.get('year_ID', '')}</td>
                <td>{s.get('team_ID', '')}</td>
                <td>{s.get('level', '')}</td>
                <td>{s.get('G', '')}</td>
                <td>{s.get('IP', '')}</td>
                <td>{s.get('earned_run_avg', '')}</td>
                <td>{s.get('W', '')}</td>
                <td>{s.get('L', '')}</td>
                <td>{s.get('SO', '')}</td>
                <td>{s.get('whip', '')}</td>
            </tr>
        """)
    return "\n".join(rows)


def summarize_with_llm(player_info, bref_stats, prospect_results):
    """Use local Qwen model to summarize prospect info."""
    client = OpenAI(
        base_url="http://127.0.0.1:8000/v1",
        api_key="not-needed"
    )

    prospect_text = "\n".join([
        f"[{r['source']}] {r['title']}: {r['body']}"
        for r in prospect_results[:4]
    ])

    prompt = f"""Based on the following prospect information for {player_info['name']} ({player_info['position']}, {player_info['team']}), write 2-3 sentences summarizing what scouts and prospect analysts say about this player. Focus on their tools, potential, and any notable rankings.

Prospect Info:
{prospect_text}

Summary:"""

    try:
        response = client.chat.completions.create(
            model="Qwen3.5-4B-MLX-4bit",
            messages=[
                {"role": "system", "content": "You are a baseball analyst. Provide concise summaries of prospect scouting reports. Focus on facts from the provided text."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Scouting summary unavailable."


def generate_html_email(debuts_with_data):
    """Generate HTML email matching the agent style."""
    today = datetime.now().strftime("%A, %B %d, %Y at %-I:%M %p")
    date_only = datetime.now().strftime("%B %d, %Y")

    player_sections = []
    for i, player in enumerate(debuts_with_data, 1):
        bref = player.get("bref_stats", {})
        is_pitcher = player["position"] in ["P", "SP", "RP", "LHP", "RHP"]

        if is_pitcher and bref.get("pitching"):
            stats_table = f"""
                <table>
                    <thead>
                        <tr>
                            <th>Year</th>
                            <th>Team</th>
                            <th>Level</th>
                            <th>G</th>
                            <th>IP</th>
                            <th>ERA</th>
                            <th>W</th>
                            <th>L</th>
                            <th>K</th>
                            <th>WHIP</th>
                        </tr>
                    </thead>
                    <tbody>
                        {format_pitching_stats_table(bref.get('pitching', []))}
                    </tbody>
                </table>
            """
            career = bref.get("career_pitching", {})
            career_line = f"<strong>Career MiLB:</strong> {career.get('G', '?')} G, {career.get('IP', '?')} IP, {career.get('earned_run_avg', '?')} ERA, {career.get('SO', '?')} K, {career.get('whip', '?')} WHIP"
        elif bref.get("batting"):
            stats_table = f"""
                <table>
                    <thead>
                        <tr>
                            <th>Year</th>
                            <th>Team</th>
                            <th>Level</th>
                            <th>G</th>
                            <th>PA</th>
                            <th>AVG</th>
                            <th>HR</th>
                            <th>RBI</th>
                            <th>SB</th>
                            <th>OPS</th>
                        </tr>
                    </thead>
                    <tbody>
                        {format_batting_stats_table(bref.get('batting', []))}
                    </tbody>
                </table>
            """
            career = bref.get("career_batting", {})
            career_line = f"<strong>Career MiLB:</strong> {career.get('G', '?')} G, {career.get('batting_avg', '?')} AVG, {career.get('HR', '?')} HR, {career.get('RBI', '?')} RBI, {career.get('SB', '?')} SB, {career.get('onbase_plus_slugging', '?')} OPS"
        else:
            stats_table = "<p><em>Minor league stats not available</em></p>"
            career_line = ""

        # BSL availability badge
        if player.get("bsl_owned") is True:
            bsl_badge = '<span class="badge badge-owned">OWNED</span>'
        elif player.get("bsl_owned") is False:
            bsl_badge = '<span class="badge badge-available">AVAILABLE</span>'
        else:
            bsl_badge = '<span class="badge badge-unknown">BSL ?</span>'

        # Position badges from 2026 minor league fielding
        positions_2026 = bref.get("positions_2026", [])
        if positions_2026:
            position_badges = " ".join([
                f'<span class="badge badge-position">{p["pos"]} ({p["games"]}G)</span>'
                for p in positions_2026
            ])
        else:
            position_badges = f'<span class="badge badge-position">{player["position"]}</span>'

        player_sections.append(f"""
            <div class="callup-alert">
                <h3>{i}. {player['name']} {position_badges} {bsl_badge}</h3>
                <p><strong>Team:</strong> {player['team']} | <strong>Age:</strong> {player['age']} | <strong>Debut:</strong> {player['debut_date'].strftime('%B %d, %Y')}</p>
                <p><strong>Debut Performance:</strong> {player.get('debut_stats', 'N/A')}</p>

                <h4>Minor League Stats (via Baseball Reference)</h4>
                {stats_table}
                <p>{career_line}</p>

                <h4>Scouting Report</h4>
                <p>{player.get('scouting_summary', 'No scouting information available.')}</p>
            </div>
        """)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            background: white;
            border-radius: 12px;
            padding: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .header {{
            background: linear-gradient(135deg, #1e5128 0%, #4e9f3d 100%);
            padding: 25px;
            border-radius: 12px 12px 0 0;
            margin: -30px -30px 25px -30px;
            text-align: center;
        }}
        .header h1 {{
            margin: 0;
            font-size: 28px;
            font-weight: 900;
            color: white;
            letter-spacing: 1px;
        }}
        .header .subtitle {{
            color: #c8e6c9;
            font-size: 14px;
            margin-top: 8px;
        }}
        h2 {{ color: #1e5128; font-size: 20px; margin-top: 30px; border-bottom: 2px solid #4e9f3d; padding-bottom: 10px; }}
        h3 {{ color: #2d6a4f; font-size: 18px; margin-top: 20px; margin-bottom: 10px; }}
        h4 {{ color: #40916c; font-size: 15px; margin-top: 15px; margin-bottom: 8px; }}
        p {{ margin: 10px 0; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 15px 0;
            font-size: 13px;
        }}
        th {{
            background: #1e5128;
            color: white;
            padding: 10px 12px;
            text-align: left;
            font-weight: 600;
        }}
        td {{
            padding: 10px 12px;
            border-bottom: 1px solid #ddd;
        }}
        tr:nth-child(even) {{ background: #f8f9fa; }}
        tr:hover {{ background: #e8f5e9; }}
        .callup-alert {{
            background: #d4edda;
            border-left: 4px solid #28a745;
            padding: 20px;
            margin: 20px 0;
            border-radius: 0 8px 8px 0;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            margin-left: 10px;
        }}
        .badge-add {{ background: #28a745; color: white; }}
        .badge-position {{ background: #6f42c1; color: white; }}
        .badge-available {{ background: #007bff; color: white; }}
        .badge-owned {{ background: #dc3545; color: white; }}
        .badge-unknown {{ background: #6c757d; color: white; }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            font-size: 12px;
            color: #666;
            text-align: center;
        }}
        .summary-box {{
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            margin: 15px 0;
            border-radius: 0 8px 8px 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>MLB Debuts Report</h1>
            <div class="subtitle">{today}</div>
        </div>

        <div class="summary-box">
            <strong>{len(debuts_with_data)} player(s)</strong> made their MLB debut in the past two days.
        </div>

        <h2>New MLB Debuts</h2>

        {"".join(player_sections)}

        <div class="footer">
            <p>Data sources: ESPN, Baseball Reference, MLB Pipeline, Fangraphs, Baseball America</p>
            <p>Generated by MLB Debuts Tracker</p>
        </div>
    </div>
</body>
</html>
"""
    return html


def generate_text_email(debuts_with_data):
    """Generate plain text version of email."""
    lines = [
        f"MLB Debuts Report - {datetime.now().strftime('%B %d, %Y')}",
        "=" * 60,
        f"\n{len(debuts_with_data)} player(s) made their MLB debut in the past two days.\n"
    ]

    for i, player in enumerate(debuts_with_data, 1):
        # BSL status
        if player.get("bsl_owned") is True:
            bsl_status = "[OWNED]"
        elif player.get("bsl_owned") is False:
            bsl_status = "[AVAILABLE]"
        else:
            bsl_status = "[BSL ?]"

        # 2026 positions
        bref = player.get("bref_stats", {})
        positions_2026 = bref.get("positions_2026", [])
        if positions_2026:
            pos_str = ", ".join([f"{p['pos']}({p['games']}G)" for p in positions_2026])
        else:
            pos_str = player['position']

        lines.append(f"\n{i}. {player['name']} [{pos_str}] ({player['team']}) {bsl_status}")
        lines.append(f"   Age: {player['age']} | Debut: {player['debut_date'].strftime('%B %d, %Y')}")
        lines.append(f"   Debut Performance: {player.get('debut_stats', 'N/A')}")

        bref = player.get("bref_stats", {})
        is_pitcher = player["position"] in ["P", "SP", "RP", "LHP", "RHP"]

        if is_pitcher and bref.get("career_pitching"):
            c = bref["career_pitching"]
            lines.append(f"   Career MiLB: {c.get('G', '?')} G, {c.get('IP', '?')} IP, {c.get('earned_run_avg', '?')} ERA, {c.get('SO', '?')} K")
        elif bref.get("career_batting"):
            c = bref["career_batting"]
            lines.append(f"   Career MiLB: {c.get('G', '?')} G, {c.get('batting_avg', '?')} AVG, {c.get('HR', '?')} HR, {c.get('RBI', '?')} RBI, {c.get('onbase_plus_slugging', '?')} OPS")

        lines.append(f"   Scouting: {player.get('scouting_summary', 'N/A')}")

    lines.append("\n" + "=" * 60)
    lines.append("Sources: ESPN, Baseball Reference, MLB Pipeline, Fangraphs, Baseball America")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("MLB DEBUTS TRACKER")
    print("=" * 60)

    print("\nFetching ESPN MLB debuts page...")
    soup = fetch_espn_debuts()

    print("Parsing recent debuts (last 2 days)...")
    debuts = parse_debuts(soup, days_back=2)

    if not debuts:
        print("\nNo debuts found in the past 2 days.")
        return None

    print(f"\nFound {len(debuts)} recent debut(s):")
    for d in debuts:
        print(f"  - {d['name']} ({d['team']}, {d['position']}, Age {d['age']})")

    debuts_with_data = []

    for player in debuts:
        print(f"\n{'='*50}")
        print(f"Processing: {player['name']}")
        print(f"{'='*50}")

        print("  Searching Baseball Reference...")
        player_id = find_bref_player_id(player["name"])
        time.sleep(1)

        bref_stats = None
        if player_id:
            print(f"  Found player ID: {player_id}")
            bref_stats = get_minor_league_stats(player_id)
            time.sleep(1)
        else:
            print("  Player not found on Baseball Reference")

        print("  Searching prospect rankings...")
        prospect_results = search_prospect_info(player["name"], player["team"])

        print("  Checking BSL ownership...")
        bsl_status = check_bsl_ownership(player["name"])
        if bsl_status["owned"]:
            print(f"    OWNED in BSL")
        elif bsl_status["owned"] is False:
            print(f"    AVAILABLE in BSL")
        else:
            print(f"    Unable to check BSL status")

        print("  Generating scouting summary...")
        scouting_summary = summarize_with_llm(player, bref_stats, prospect_results)

        debuts_with_data.append({
            **player,
            "bref_stats": bref_stats or {},
            "scouting_summary": scouting_summary,
            "bsl_owned": bsl_status["owned"],
            "bsl_mlbid": bsl_status["mlbid"]
        })
        print("  Done!")

    print("\n" + "=" * 60)
    print("GENERATING EMAIL")
    print("=" * 60)

    html_email = generate_html_email(debuts_with_data)
    text_email = generate_text_email(debuts_with_data)

    output_html = "debuts_email.html"
    output_txt = "debuts_email.txt"

    with open(output_html, "w") as f:
        f.write(html_email)
    with open(output_txt, "w") as f:
        f.write(text_email)

    print(f"\nEmail saved to: {output_html} and {output_txt}")

    print("\n" + "=" * 60)
    print("SENDING EMAIL")
    print("=" * 60)

    subject = f"MLB Debuts Report - {datetime.now().strftime('%B %d, %Y')}"
    send_email(subject, html_email, text_email)

    print("\n" + text_email)

    return html_email


if __name__ == "__main__":
    main()
