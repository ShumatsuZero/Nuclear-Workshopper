import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import threading
import concurrent.futures
from queue import Queue
from datetime import datetime

# Global variables for pause/resume control
is_paused = False
pause_event = threading.Event()
scraper_thread = None
current_username = ""
current_page = 1
current_items = []
error_queue = Queue()
rate_limit_detected = False
pending_items = []  # Store items that need to be processed after resume
auto_paused = False  # Track if we auto-paused due to error
processing_pending = False  # Track if we're processing pending items


def resume_scraping():
    """Resume scraping after auto-pause"""
    global is_paused, pause_event, rate_limit_detected, auto_paused, processing_pending
    if is_paused:
        # Resume scraping
        is_paused = False
        pause_event.clear()
        console_output.insert(tk.END, f"\n[{datetime.now().strftime('%H:%M:%S')}] Resuming scraping...\n")
        console_output.see(tk.END)
        resume_button.config(state="disabled")
        auto_paused = False  # Reset auto-pause flag

        # If we were paused due to rate limit, reset the flag
        if rate_limit_detected:
            console_output.insert(tk.END,
                                  f"[{datetime.now().strftime('%H:%M:%S')}] Rate limit flag cleared. Will continue scanning.\n")
            console_output.see(tk.END)
            rate_limit_detected = False


def handle_auto_pause():
    """Handle auto-pause from main thread"""
    global is_paused, pause_event, auto_paused
    is_paused = True
    pause_event.set()
    resume_button.config(state="normal")
    console_output.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] Click 'Resume' to continue when ready.\n")
    console_output.see(tk.END)


def check_for_errors():
    """Check for errors in the queue and handle them"""
    global rate_limit_detected, auto_paused
    try:
        while not error_queue.empty():
            error_msg = error_queue.get_nowait()
            console_output.insert(tk.END, f"\n[{datetime.now().strftime('%H:%M:%S')}] ERROR: {error_msg}\n")
            console_output.see(tk.END)

            if "429" in error_msg or "Too Many Requests" in error_msg:
                console_output.insert(tk.END,
                                      f"\n[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Steam rate limit detected! Auto-pausing...\n")
                console_output.insert(tk.END,
                                      f"[{datetime.now().strftime('%H:%M:%S')}] Please wait 5 minutes before resuming.\n")
                console_output.see(tk.END)
                rate_limit_detected = True
                auto_paused = True  # Set auto-pause flag
                if not is_paused:
                    # Schedule auto-pause on main thread
                    root.after(0, handle_auto_pause)

    except:
        pass

    # Schedule next check
    if scraper_thread and scraper_thread.is_alive():
        root.after(100, check_for_errors)


def process_item_batch(item_batch, page_num, is_pending=False):
    """Process a batch of items with proper error handling and pause support"""
    global pending_items, current_items

    results = []

    for i, item in enumerate(item_batch):
        # Check for pause or rate limit
        while pause_event.is_set() or rate_limit_detected:
            if not scraper_thread or not scraper_thread.is_alive():
                return results
            time.sleep(0.5)

        try:
            thread_name = threading.current_thread().name

            if is_pending:
                # For pending items, item is actually a tuple (item_data, page_num, item_index)
                item_data = item
                item_obj = item_data[0]
                page_num = item_data[1]
                item_index = item_data[2]

                name_tag = item_obj.select_one('.workshopItemTitle')
                name = name_tag.text.strip() if name_tag else 'Unknown'

                item_link_tag = item_obj.select_one('a')
                item_link = item_link_tag['href'] if item_link_tag else None

                batch_info = f"Pending Item {item_index + 1}"
            else:
                # For regular items
                name_tag = item.select_one('.workshopItemTitle')
                name = name_tag.text.strip() if name_tag else 'Unknown'

                item_link_tag = item.select_one('a')
                item_link = item_link_tag['href'] if item_link_tag else None

                batch_info = f"Item {i + 1}/{len(item_batch)}"

            if item_link:
                stats = fetch_item_details(item_link)
                if stats:
                    stats = {"Name": name, **stats}

                    displayType = 'unknown'
                    if stats['Type'] == 'Mission':
                        displayType = 'custom mission'
                    if stats['Type'] == 'Aircraft Livery':
                        displayType = 'livery'
                        if stats['Airframe'] != 'Unknown':
                            displayType = f"{stats['Airframe']} livery"

                    if is_pending:
                        root.after(0, lambda
                            msg=f"[{datetime.now().strftime('%H:%M:%S')}] [Retry Page {page_num}, {batch_info}]: Found {displayType} {stats['Name']}.": update_console(
                            msg))
                    else:
                        root.after(0, lambda
                            msg=f"[{datetime.now().strftime('%H:%M:%S')}] [Page {page_num}, {batch_info}]: Found {displayType} {stats['Name']}.": update_console(
                            msg))
                    results.append(stats)
                else:
                    # If fetch_item_details returns None (due to error), add to pending
                    if is_pending:
                        pending_items.append(item)  # Keep as tuple
                    else:
                        pending_items.append((item, page_num, i))
                    if is_pending:
                        root.after(0, lambda: update_console(
                            f"[{datetime.now().strftime('%H:%M:%S')}] Pending item {item_index + 1} failed again, keeping in retry queue."))
                    else:
                        root.after(0, lambda: update_console(
                            f"[{datetime.now().strftime('%H:%M:%S')}] Item {i + 1} failed, added to retry queue."))

        except Exception as e:
            # If any error occurs, add to pending items for retry
            if is_pending:
                pending_items.append(item)  # Keep as tuple
                error_queue.put(f"Error retrying pending item on page {page_num}: {str(e)}")
            else:
                pending_items.append((item, page_num, i))
                error_queue.put(f"Error processing item {i + 1} on page {page_num}: {str(e)}")

    return results


def fetch_workshop_items(base_url, username):
    global current_page, current_items, rate_limit_detected, pending_items, auto_paused, processing_pending

    page_number = current_page
    items = current_items.copy()

    while True:
        # Check if paused
        if pause_event.is_set():
            if auto_paused:
                console_output.insert(tk.END,
                                      f"\n[{datetime.now().strftime('%H:%M:%S')}] ⏸️ Auto-paused on page {page_number} due to rate limit\n")
            else:
                console_output.insert(tk.END,
                                      f"\n[{datetime.now().strftime('%H:%M:%S')}] ⏸️ Paused on page {page_number}\n")
            console_output.see(tk.END)
            while pause_event.is_set():
                time.sleep(0.5)
                if not scraper_thread or not scraper_thread.is_alive():
                    console_output.insert(tk.END,
                                          f"\n[{datetime.now().strftime('%H:%M:%S')}] Scraping thread terminated.\n")
                    console_output.see(tk.END)
                    return items

        # Check if rate limit was detected - we'll continue after resume
        if rate_limit_detected:
            # Wait for resume and rate limit flag to be cleared
            console_output.insert(tk.END,
                                  f"\n[{datetime.now().strftime('%H:%M:%S')}] Rate limit detected, waiting for resume...\n")
            console_output.see(tk.END)
            while rate_limit_detected:
                time.sleep(0.5)
                if not scraper_thread or not scraper_thread.is_alive():
                    return items
            console_output.insert(tk.END,
                                  f"\n[{datetime.now().strftime('%H:%M:%S')}] Continuing from page {page_number} after rate limit...\n")
            console_output.see(tk.END)

        # FIRST: Process any pending items before new ones
        if pending_items and not processing_pending:
            processing_pending = True
            console_output.insert(tk.END,
                                  f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Processing {len(pending_items)} pending items first...\n")
            console_output.see(tk.END)

            # Process pending items in batches
            retry_items = pending_items.copy()
            pending_items = []  # Clear pending items list

            # Group pending items by page for better organization
            pending_by_page = {}
            for item_data in retry_items:
                if isinstance(item_data, tuple) and len(item_data) == 3:
                    item_obj, page_num, item_index = item_data
                    if page_num not in pending_by_page:
                        pending_by_page[page_num] = []
                    pending_by_page[page_num].append(item_data)

            # Process pending items from each page
            for page_num in sorted(pending_by_page.keys()):
                page_pending_items = pending_by_page[page_num]
                console_output.insert(tk.END,
                                      f"\n[{datetime.now().strftime('%H:%M:%S')}] Processing {len(page_pending_items)} pending items from page {page_num}...\n")
                console_output.see(tk.END)

                # Process in batches of 4
                batch_size = 4
                for i in range(0, len(page_pending_items), batch_size):
                    batch = page_pending_items[i:i + batch_size]

                    # Check for pause before processing batch
                    if pause_event.is_set() or rate_limit_detected:
                        # Add unprocessed items back to pending
                        pending_items.extend(batch)
                        console_output.insert(tk.END,
                                              f"\n[{datetime.now().strftime('%H:%M:%S')}] Added pending batch {i // batch_size + 1} back to queue due to pause\n")
                        console_output.see(tk.END)
                        continue

                    console_output.insert(tk.END,
                                          f"\n[{datetime.now().strftime('%H:%M:%S')}] Processing pending batch {i // batch_size + 1}/{(len(page_pending_items) - 1) // batch_size + 1}...\n")
                    console_output.see(tk.END)

                    batch_results = process_item_batch(batch, page_num, is_pending=True)
                    items.extend(batch_results)

                    # Update current items after each batch
                    current_items = items.copy()

            console_output.insert(tk.END,
                                  f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Finished processing pending items\n")
            console_output.see(tk.END)
            processing_pending = False

        url = f"{base_url}&p={page_number}"

        try:
            console_output.insert(tk.END,
                                  f"\n[{datetime.now().strftime('%H:%M:%S')}] 📄 Loading page {page_number}...\n")
            console_output.see(tk.END)

            # Add longer delay when resuming from rate limit
            delay_time = calculate_delay(page_number)
            if delay_time > 1:
                console_output.insert(tk.END,
                                      f"[{datetime.now().strftime('%H:%M:%S')}] Adding {delay_time}s delay to avoid rate limiting...\n")
                console_output.see(tk.END)
                time.sleep(delay_time)

            response = requests.get(url, timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            workshop_items = soup.select('.workshopItem')

            if not workshop_items:
                console_output.insert(tk.END,
                                      f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ No more items found. Total pages: {page_number - 1}\n")
                console_output.see(tk.END)

                # Process any remaining pending items before finishing
                if pending_items:
                    console_output.insert(tk.END,
                                          f"\n[{datetime.now().strftime('%H:%M:%S')}] Processing remaining {len(pending_items)} pending items...\n")
                    console_output.see(tk.END)
                    process_final_pending_items(items)

                break

            # Process items in smaller batches to handle pauses better
            batch_size = 4  # Same as max_workers
            for i in range(0, len(workshop_items), batch_size):
                batch = workshop_items[i:i + batch_size]

                # Check for pause before processing batch
                if pause_event.is_set() or rate_limit_detected:
                    # Add unprocessed items to pending
                    for j, item in enumerate(batch):
                        pending_items.append((item, page_number, i + j))
                    console_output.insert(tk.END,
                                          f"\n[{datetime.now().strftime('%H:%M:%S')}] Added batch {i // batch_size + 1} to pending due to pause\n")
                    console_output.see(tk.END)
                    continue

                console_output.insert(tk.END,
                                      f"\n[{datetime.now().strftime('%H:%M:%S')}] Processing batch {i // batch_size + 1}/{(len(workshop_items) - 1) // batch_size + 1}...\n")
                console_output.see(tk.END)

                batch_results = process_item_batch(batch, page_number, is_pending=False)
                items.extend(batch_results)

                # Update current items after each batch
                current_items = items.copy()

            console_output.insert(tk.END,
                                  f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Completed page {page_number}, found {len(workshop_items)} items.\n")
            console_output.see(tk.END)

            # Save current state
            current_page = page_number + 1
            current_items = items.copy()

            page_number += 1

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                error_queue.put(f"429 Too Many Requests on page {page_number}. URL: {url}")
                # Don't return here - let the error handler pause us
                # Wait for resume
                while pause_event.is_set() or rate_limit_detected:
                    time.sleep(0.5)
                    if not scraper_thread or not scraper_thread.is_alive():
                        return items
                # After resume, continue with next iteration
                continue
            else:
                error_queue.put(f"HTTP Error {e.response.status_code} on page {page_number}: {str(e)}")
                return items
        except requests.exceptions.RequestException as e:
            error_queue.put(f"Request failed on page {page_number}: {str(e)}")
            # Wait for resume
            while pause_event.is_set():
                time.sleep(0.5)
                if not scraper_thread or not scraper_thread.is_alive():
                    return items
            continue
        except Exception as e:
            error_queue.put(f"Unexpected error on page {page_number}: {str(e)}")
            return items

    return items


def process_final_pending_items(items_list):
    """Process any remaining pending items at the end"""
    global pending_items

    if not pending_items:
        return

    console_output.insert(tk.END,
                          f"\n[{datetime.now().strftime('%H:%M:%S')}] Final retry of {len(pending_items)} failed items...\n")
    console_output.see(tk.END)

    # Process remaining pending items
    retry_items = pending_items.copy()
    pending_items = []  # Clear pending items

    for item_data in retry_items:
        # Check for pause
        while pause_event.is_set() or rate_limit_detected:
            if not scraper_thread or not scraper_thread.is_alive():
                return
            time.sleep(0.5)

        try:
            if isinstance(item_data, tuple) and len(item_data) == 3:
                item, page_num, item_index = item_data

                name_tag = item.select_one('.workshopItemTitle')
                name = name_tag.text.strip() if name_tag else 'Unknown'

                item_link_tag = item.select_one('a')
                item_link = item_link_tag['href'] if item_link_tag else None

                if item_link:
                    stats = fetch_item_details(item_link)
                    if stats:
                        stats = {"Name": name, **stats}

                        displayType = 'unknown'
                        if stats['Type'] == 'Mission':
                            displayType = 'custom mission'
                        if stats['Type'] == 'Aircraft Livery':
                            displayType = 'livery'
                            if stats['Airframe'] != 'Unknown':
                                displayType = f"{stats['Airframe']} livery"

                        root.after(0, lambda
                            msg=f"[{datetime.now().strftime('%H:%M:%S')}] [Final Retry Page {page_num}, Item {item_index + 1}]: Found {displayType} {stats['Name']}.": update_console(
                            msg))
                        items_list.append(stats)
                    else:
                        # If still fails, add back to pending
                        pending_items.append(item_data)

        except Exception as e:
            # If still fails, add back to pending
            pending_items.append(item_data)
            error_queue.put(f"Error in final retry: {str(e)}")

    if pending_items:
        console_output.insert(tk.END,
                              f"\n[{datetime.now().strftime('%H:%M:%S')}] ⚠️ {len(pending_items)} items still pending after final retry\n")
        console_output.see(tk.END)
    else:
        console_output.insert(tk.END, f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ All items processed successfully\n")
        console_output.see(tk.END)


def calculate_delay(page_number):
    """Calculate delay based on page number to avoid rate limiting"""
    if page_number > 15:
        return 3.0
    elif page_number > 10:
        return 2.5
    elif page_number > 5:
        return 2.0
    else:
        return 1.0


def update_console(message):
    """Thread-safe console update"""
    console_output.insert(tk.END, f"\n{message}\n")
    console_output.see(tk.END)


def fetch_item_details(item_url):
    # Check pause before making request
    if pause_event.is_set() or rate_limit_detected:
        return None

    try:
        # Add small delay between item detail requests
        time.sleep(0.5)

        response = requests.get(item_url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        visitors = get_stat(soup, 'Unique Visitors')
        subscribers = get_stat(soup, 'Current Subscribers')
        favorites = get_stat(soup, 'Current Favorites')
        awards = get_awards(soup)
        item_type = get_item_type(soup)
        comments = get_comments_count(soup)
        file_size, date_posted, date_updated = get_file_info(soup)
        num_changes = get_num_changes(soup)
        description = get_description(soup)

        airframe = ""
        if item_type == "Aircraft Livery":
            airframe = get_airframe(soup, description)

        return {
            'Type': item_type,
            'Airframe': airframe,
            'Visitors': int(visitors.replace(",", "")) if visitors != '0' else 0,
            'Subscribers': int(subscribers.replace(",", "")) if subscribers != '0' else 0,
            'Favorites': int(favorites.replace(",", "")) if favorites != '0' else 0,
            'Awards': int(awards) if awards != '0' else 0,
            'Comments': int(comments.replace(",", "")) if comments != '0' else 0,
            'Changes': int(num_changes.replace(",", "")) if num_changes != '0' else 0,
            'File Size': file_size.replace(" ", ""),
            'Uploaded': fix_date_format(date_posted),
            'Updated': fix_date_format(date_updated),
            'Description': description
        }

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            error_queue.put(f"429 Too Many Requests on item: {item_url}")
        else:
            error_queue.put(f"HTTP Error {e.response.status_code} on item: {str(e)}")
    except Exception as e:
        error_queue.put(f"Error fetching item details: {str(e)}")

    return None


def fix_date_format(date_string):
    """Fix date strings that are missing the year"""
    if date_string in ['Unknown', '? KB']:
        return date_string

    current_year = datetime.now().year

    # Check if date already has a 4-digit year
    import re
    if re.search(r'\b(19|20)\d{2}\b', date_string):
        # Date already has a year, just standardize format
        return date_string.replace(' @ ', ', ')

    # Date doesn't have a year, add current year
    if ' @ ' in date_string:
        # Format: "1 Jan @ 12:20pm"
        parts = date_string.split(' @ ')
        return f"{parts[0]}, {current_year}, {parts[1]}"
    elif ', ' in date_string and not any(str(year) in date_string for year in range(1900, 2100)):
        # Format: "1 Jan, 12:20pm" (comma but no year)
        parts = date_string.split(', ')
        if len(parts) >= 2:
            return f"{parts[0]}, {current_year}, {', '.join(parts[1:])}"

    # If we can't parse it, return as-is
    return date_string.replace(' @ ', ', ')


def get_stat(soup, label):
    try:
        stats_table = soup.find('table', class_='stats_table')
        if stats_table:
            rows = stats_table.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if len(cells) == 2 and label in cells[1].text:
                    return cells[0].text.strip()
    except Exception as e:
        error_queue.put(f"Error fetching {label}: {e}")
    return '0'


def get_awards(soup):
    try:
        award_container = soup.find('div', class_='review_award_ctn')
        if not award_container:
            return '0'

        awards = award_container.find_all('div', class_='review_award tooltip')
        total_awards = sum(int(award['data-reactioncount']) for award in awards if award.has_attr('data-reactioncount'))
        return str(total_awards)
    except Exception as e:
        error_queue.put(f"Error fetching awards: {e}")
        return '0'


def get_item_type(soup):
    try:
        details_block = soup.find('div', class_='rightDetailsBlock')
        if not details_block:
            return 'Unknown'

        item_type_tag = details_block.find('a')
        return item_type_tag.text.strip() if item_type_tag else 'Unknown'
    except Exception as e:
        error_queue.put(f"Error fetching item type: {e}")
        return 'Unknown'


def get_comments_count(soup):
    try:
        comment_section = soup.find('div', class_='commentthread_header_and_count')
        if comment_section:
            count_label = comment_section.find('span', class_='ellipsis commentthread_count_label')
            if count_label:
                count_span = count_label.find('span')
                return count_span.text.strip() if count_span else '0'
    except Exception as e:
        error_queue.put(f"Error fetching comments: {e}")
    return '0'


def get_file_info(soup):
    try:
        stats_container = soup.find('div', class_='detailsStatsContainerRight')
        if stats_container:
            stats = stats_container.find_all('div', class_='detailsStatRight')
            if len(stats) >= 2:
                file_size = stats[0].text.strip()
                date_posted = stats[1].text.strip()
                date_updated = date_posted
                if len(stats) >= 3:
                    date_updated = stats[2].text.strip()

                return file_size, date_posted, date_updated
    except Exception as e:
        error_queue.put(f"Error fetching file info: {e}")
    return '? KB', 'Unknown', 'Unknown'


def get_num_changes(soup):
    try:
        change_note = soup.find('div', class_='detailsStatNumChangeNotes')
        if change_note:
            text = change_note.text.strip()
            return text[:-26] if text.endswith("( view )") else '0'
    except Exception as e:
        error_queue.put(f"Error fetching changes: {e}")
    return '0'


def get_description(soup):
    try:
        description_div = soup.find('div', id='highlightContent', class_='workshopItemDescription')
        if description_div:
            return description_div.text.strip()
    except Exception as e:
        error_queue.put(f"Error fetching description: {e}")
    return 'No description.'


def get_airframe(soup, description):
    airframes = {
        "ci-22": "CI-22",
        "cricket": "CI-22",
        "t/a-30": "T/A-30",
        "compass": "T/A-30",
        "a-19": "A-19",
        "brawler": "A-19",
        "uh-90": "UH-90",
        "ibis": "UH-90",
        "sah-46": "SAH-46",
        "chicane": "SAH-46",
        "fs-12": "FS-12",
        "revoker": "FS-12",
        "fs-20": "FS-20",
        "vortex": "FS-20",
        "kr-67": "KR-67",
        "ifrit": "KR-67",
        "vl-49": "VL-49",
        "tarantula": "VL-49",
        "ew-25": "EW-25",
        "medusa": "EW-25",
        "sfb-81": "SFB-81",
        "darkreach": "SFB-81"
    }

    try:
        description = description.lower()
        for airframe in airframes:
            if airframe in description:
                return airframes[airframe]
    except Exception as e:
        error_queue.put(f"Error fetching airframe: {e}")
    return "Unknown"


def save_to_excel(data, filename):
    console_output.insert(tk.END, f"\n[{datetime.now().strftime('%H:%M:%S')}] All items processed, saving...\n")
    console_output.see(tk.END)
    df = pd.DataFrame(data)
    cols = df.columns.tolist()
    cols2 = cols
    cols[0], cols[1], cols[2], cols[3], cols[4], cols[5], cols[6], cols[7], cols[8], cols[9], cols[10], cols[11] = \
        cols2[11], cols2[4], cols2[0], cols2[1], cols2[2], cols2[3], cols2[5], cols2[9], cols2[6], cols2[7], cols2[8], \
        cols2[10]
    df = df[cols]
    df.to_excel(filename, index=False)
    console_output.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] Data saved to {filename}!\n")
    console_output.see(tk.END)


def save_to_excel2(data):
    console_output.insert(tk.END,
                          f"\n[{datetime.now().strftime('%H:%M:%S')}] All items processed, attempting to save file...\n")
    console_output.see(tk.END)
    filename = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel files", "*.xlsx")])
    if filename:
        df = pd.DataFrame(data)
        df.to_excel(filename, index=False)
        if not filename == "":
            console_output.insert(tk.END, f"\n[{datetime.now().strftime('%H:%M:%S')}] Data exported as {filename}\n")
            console_output.see(tk.END)


def main_process(username):
    global current_username, current_page, current_items, scraper_thread, rate_limit_detected, pending_items, auto_paused, processing_pending

    if username.isdigit():
        user_url = f"https://steamcommunity.com/profiles/{username}/myworkshopfiles/?appid=2168680"
        console_output.insert(tk.END,
                              f"\n[{datetime.now().strftime('%H:%M:%S')}] Steam User ID detected, searching...\n")
        console_output.see(tk.END)
    else:
        user_url = f"https://steamcommunity.com/id/{username}/myworkshopfiles/?appid=2168680"
        console_output.insert(tk.END,
                              f"\n[{datetime.now().strftime('%H:%M:%S')}] Steam CustomLink name detected, searching...\n")
        console_output.see(tk.END)

    try:
        current_username = username
        workshop_items = fetch_workshop_items(user_url, username)

        if workshop_items:
            save_to_excel2(workshop_items)
            messagebox.showinfo("Success", f"Data saved successfully for {username}!")
            console_output.insert(tk.END, f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Data saved successfully.\n")
            console_output.see(tk.END)
        else:
            messagebox.showinfo("No Data", "No items were collected.")

    except Exception as e:
        messagebox.showerror("Error", f"An error occurred: {e}")
    finally:
        # Reset state
        current_page = 1
        current_items = []
        rate_limit_detected = False
        pending_items = []
        auto_paused = False
        processing_pending = False
        # Update button states from main thread
        root.after(0, lambda: start_button.config(state="normal"))
        root.after(0, lambda: resume_button.config(state="disabled"))


def run_scraper():
    global scraper_thread

    username = username_entry.get().strip()
    if not username:
        messagebox.showwarning("Input Error", "Please enter a Steam username.")
    else:
        # Reset state for new scrape
        global current_page, current_items, rate_limit_detected, pending_items, auto_paused, processing_pending
        current_page = 1
        current_items = []
        rate_limit_detected = False
        pending_items = []
        auto_paused = False
        processing_pending = False

        console_output.insert(tk.END,
                              f"\n[{datetime.now().strftime('%H:%M:%S')}] 🚀 Fetching workshop items from {username}...\n")
        console_output.see(tk.END)
        start_button.config(state="disabled")
        resume_button.config(state="disabled")

        scraper_thread = threading.Thread(target=main_process, args=(username,), daemon=True)
        scraper_thread.start()

        # Start error checking
        root.after(100, check_for_errors)

        # Monitor thread completion to re-enable buttons
        def check_thread():
            if scraper_thread.is_alive():
                root.after(500, check_thread)
            else:
                start_button.config(state="normal")
                resume_button.config(state="disabled")
                is_paused = False
                pause_event.clear()

        root.after(500, check_thread)


def reset_scraper():
    """Reset the scraper to initial state"""
    global is_paused, pause_event, scraper_thread, current_username, current_page, current_items, rate_limit_detected, pending_items, auto_paused, processing_pending

    is_paused = False
    pause_event.clear()
    current_page = 1
    current_items = []
    current_username = ""
    rate_limit_detected = False
    pending_items = []
    auto_paused = False
    processing_pending = False

    start_button.config(state="normal")
    resume_button.config(state="disabled")
    console_output.insert(tk.END, f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Scraper reset to initial state.\n")
    console_output.see(tk.END)


def clear_console():
    """Clear the console output"""
    console_output.delete(1.0, tk.END)
    console_output.insert(tk.END,
                          f"Nuclear Workshopper 0.2\nby offiry, fixed and updated by Shumatsu\n\nInstructions:\n1. Enter Steam username\n2. Click 'Start'\n3. Program auto-pauses if rate limited\n4. Manually click 'Resume' after 5 minutes\n5. Use 'Reset' to start over\n\n[{datetime.now().strftime('%H:%M:%S')}] Console cleared.\n")


# Create GUI
root = tk.Tk()
root.title("Workshopper 0.2")

root.geometry("1000x600")

# Configure styles
style = ttk.Style()
style.configure("Title.TLabel", font=("Arial", 14, "bold"))
style.configure("Status.TLabel", font=("Arial", 10))

frame = ttk.Frame(root, padding=10)
frame.pack(fill="both", expand=True)

# Title
ttk.Label(frame, text="Nuclear Workshopper",
          style="Title.TLabel").pack(pady=10)

# Input section
input_frame = ttk.Frame(frame)
input_frame.pack(pady=10, fill="x")

ttk.Label(input_frame, text="Enter Steam User ID (e.g. 123456789) or Steam CustomLink (e.g. offiry)").pack(anchor="w")
username_entry = ttk.Entry(input_frame, width=60)
username_entry.pack(pady=5, fill="x")

# Button frame
button_frame = ttk.Frame(frame)
button_frame.pack(pady=10, fill="x")

start_button = ttk.Button(button_frame, text="Start", command=run_scraper)
start_button.pack(side="left", padx=5)

resume_button = ttk.Button(button_frame, text="Resume", command=resume_scraping, state="disabled")
resume_button.pack(side="left", padx=5)

reset_button = ttk.Button(button_frame, text="Reset", command=reset_scraper)
reset_button.pack(side="left", padx=5)

clear_button = ttk.Button(button_frame, text="Clear Console", command=clear_console)
clear_button.pack(side="left", padx=5)

# Console output frame with scrollbar
console_frame = ttk.LabelFrame(frame, text="Console Output", padding=5)
console_frame.pack(fill="both", expand=True, pady=10)

# Create a Text widget with scrollbar
console_text_frame = ttk.Frame(console_frame)
console_text_frame.pack(fill="both", expand=True)

# Vertical Scrollbar
v_scrollbar = ttk.Scrollbar(console_text_frame)
v_scrollbar.pack(side="right", fill="y")

# Horizontal Scrollbar
h_scrollbar = ttk.Scrollbar(console_text_frame, orient="horizontal")
h_scrollbar.pack(side="bottom", fill="x")

# Text widget
console_output = tk.Text(console_text_frame, wrap="word",
                         yscrollcommand=v_scrollbar.set,
                         xscrollcommand=h_scrollbar.set,
                         font=("Consolas", 10),
                         bg="black", fg="white",
                         height=20)
console_output.pack(side="left", fill="both", expand=True)

# Configure scrollbars
v_scrollbar.config(command=console_output.yview)
h_scrollbar.config(command=console_output.xview)

# Add initial text
clear_console()

# Status bar
status_frame = ttk.Frame(frame)
status_frame.pack(fill="x", pady=5)

status_text = tk.StringVar(value="Status: Ready")
status_label = ttk.Label(status_frame, textvariable=status_text, style="Status.TLabel")
status_label.pack(side="left")

# Progress bar
progress_bar = ttk.Progressbar(status_frame, mode='indeterminate', length=200)
progress_bar.pack(side="right", padx=10)


def update_status():
    if auto_paused:
        status_text.set(f"Status: Auto-paused - {len(pending_items)} pending items - Click 'Resume'")
        progress_bar.stop()
    elif is_paused:
        status_text.set(f"Status: Paused - {len(pending_items)} pending items")
        progress_bar.stop()
    elif processing_pending:
        status_text.set(f"Status: Processing pending items ({len(pending_items)} remaining)")
        if progress_bar['mode'] == 'indeterminate':
            progress_bar.start(10)
    elif scraper_thread and scraper_thread.is_alive():
        status_text.set(
            f"Status: Running - Page {current_page}, Items: {len(current_items)}, Pending: {len(pending_items)}")
        if progress_bar['mode'] == 'indeterminate':
            progress_bar.start(10)
    else:
        status_text.set("Status: Ready")
        progress_bar.stop()
    root.after(1000, update_status)


# Start status update
root.after(1000, update_status)

# Make window resizable
root.columnconfigure(0, weight=1)
root.rowconfigure(0, weight=1)
frame.columnconfigure(0, weight=1)
frame.rowconfigure(4, weight=1)  # Console frame row

root.mainloop()
