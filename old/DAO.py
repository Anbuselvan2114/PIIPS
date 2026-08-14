import os
import json
import requests

from config import load_settings
# =====================================================
# SESSION (reuse for performance)
# =====================================================
session = requests.Session()

# =====================================================
# GET BASE URL
# =====================================================
def get_base_url():
    settings = load_settings()
    return settings.get("BASE_SERVER_URL", "").rstrip("/")

base_url = get_base_url()
# =====================================================
# EXTRACT UNIQUE JSON FIELDS
# =====================================================
def extract_unique_fields(output_folder):
    unique_fields = set()

    for file in os.listdir(output_folder):
        if file.endswith(".json"):
            file_path = os.path.join(output_folder, file)

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Top-level fields
                for key in data.keys():
                    unique_fields.add(key)

                # Items
                for item in data.get("items", []):
                    if isinstance(item, dict):
                        for k in item.keys():
                            unique_fields.add("items." + k)

                # Tax
                for tax in data.get("tax_total", []):
                    if isinstance(tax, dict):
                        for k in tax.keys():
                            unique_fields.add("tax_total." + k)

            except Exception as e:
                print("❌ Error reading JSON:", file, str(e))

    return sorted(unique_fields)


# =====================================================
# HSN DETAILS API
# =====================================================
def get_hsn_details(items):

    base_url = get_base_url()
    url = f"{base_url}/api/Purchase/GetHSNDetails"
    print(url)
    try:
        print("🚀 get_hsn_details CALLED")

        if not items:
            return {}

        # -----------------------------
        # Normalize input
        # -----------------------------
        if isinstance(items, dict):
            items = items.get("PartViewModelList") or []

        part_model_list = []

        for item in items:
            if not isinstance(item, dict):
                continue

            part_spec = str(
                item.get("PartSpecification") or item.get("Description") or ""
            ).strip()

            po_no = str(
                item.get("PurchaseOrderNo") or item.get("buyer_order_no") or ""
            ).strip()

            if not part_spec:
                continue

            part_model_list.append({
                "PartSpecification": part_spec,
                "PurchaseOrderNo": po_no
            })

        # remove duplicates
        part_model_list = list({
            (x["PartSpecification"], x["PurchaseOrderNo"]): x
            for x in part_model_list
        }.values())

        print("📤 HSN API PAYLOAD:", part_model_list)

        response = session.post(url, json=part_model_list, timeout=60)
        print("📡 STATUS:", response.status_code)
        response.raise_for_status()

        data = response.json()
        print("📥 RAW RESPONSE:", data)

        # -----------------------------
        # Normalize result list
        # -----------------------------
        if isinstance(data, dict):
            result = (
                data.get("PartViewModelList")
                or data.get("PartModelList")
                or data.get("Data")
                or data.get("Items")
                or []
            )
        elif isinstance(data, list):
            result = data
        else:
            result = []

        # -----------------------------
        # BUILD FINAL MAP (KEY = PO + PART)
        # -----------------------------
        hsn_map = {}

        for row in result:
            if not isinstance(row, dict):
                continue

            po = str(row.get("PurchaseOrderNo", "")).strip().lower()
            part = str(row.get("PartSpecification", "")).strip().lower()

            if not po or not part:
                continue

            hsn_map[(po, part)] = {
                "PurchaseOrderNo": row.get("PurchaseOrderNo", ""),
                "PartSpecification": row.get("PartSpecification", ""),
                "ProductNo": row.get("ProductNo") or row.get("Nav_Item_No", ""),
                "HSN_Type": row.get("HSN_Type", ""),
                "HSN_Percentage_Description": row.get("HSN_Percentage_Description", ""),
                "Nav_Item_No": row.get("Nav_Item_No", ""),
                "TaxPercentage": row.get("TaxPercentage", 0),
            }

        print(f"✅ HSN MAP CREATED: {len(hsn_map)} items")

        return hsn_map

    except Exception as e:
        print("❌ HSN API error:", e)
        return {}

# =====================================================
# SPARE PURCHASE API (MAIN FIXED FUNCTION)
# =====================================================
def get_spare_purchase_items_multiple(order_numbers):
    base_url = get_base_url()
    url = f"{base_url}/api/purchase/GetSparePurchaseItem"
    print(url)
    grouped = {}

    try:
        if not order_numbers:
            print("⚠️ Empty order numbers list")
            return grouped

        payload = [
            {
                "SpareRequestOrderNumber": str(num).strip()
            }
            for num in order_numbers
            if num and str(num).strip()
        ]

        if not payload:
            print("⚠️ No valid order numbers after cleaning")
            return grouped

        print(f"🔄 Spare API Request: {len(payload)} orders")

        response = session.post(url, json=payload, timeout=60)
        response.raise_for_status()

        response_json = response.json()

        # Normalize response
        if isinstance(response_json, list):
            result = response_json
        elif isinstance(response_json, dict):
            result = response_json.get("result", [])
        else:
            print("⚠️ Unexpected API response format")
            return grouped

        if not isinstance(result, list):
            return grouped

        # Group by order number
        for row in result:
            if not isinstance(row, dict):
                continue

            key = str(row.get("SpareRequestOrderNumber", "")).strip()

            if key:
                grouped.setdefault(key, []).append(row)

        return grouped

    except requests.Timeout:
        print("❌ API timeout")
    except requests.ConnectionError:
        print("❌ Connection error")
    except requests.HTTPError as e:
        print("❌ HTTP error:", e)
    except ValueError:
        print("❌ Invalid JSON response")
    except Exception as e:
        print("❌ Unexpected error:", e)

    return grouped

def get_address_details_multiple(address_items):

    import requests
    import json

    print("\n====================================")
    print("🚀 get_address_details_multiple STARTED")
    print("====================================")

    base_url = get_base_url()
    url = f"{base_url}/api/Purchase/GetStateCodeFromAddress"

    print(url)

    try:

        if not address_items:
            print("❌ EMPTY ADDRESS ITEMS")
            return {}

        # =====================================================
        # CLEAN INPUT
        # =====================================================
        cleaned_items = []

        for x in address_items:

            if not isinstance(x, dict):
                continue

            po = str(x.get("PurchaseOrderNo", "")).strip().lower()
            buyer = str(x.get("BuyerAddress", "")).strip()
            seller = str(x.get("SellerAddress", "")).strip()

            if not po:
                continue

            cleaned_items.append({
                "PurchaseOrderNo": po,
                "BuyerAddress": buyer,
                "SellerAddress": seller
            })

        # remove duplicates
        cleaned_items = list({
            (x["PurchaseOrderNo"], x["BuyerAddress"], x["SellerAddress"]): x
            for x in cleaned_items
        }.values())

        print(f"📊 UNIQUE ADDRESS INPUT COUNT: {len(cleaned_items)}")

        # =====================================================
        # API CALL
        # =====================================================
        headers = {
            "Content-Type": "application/json"
        }

        response = requests.post(url, json=cleaned_items, headers=headers, timeout=60)

        print("====================================")
        print("📥 RAW API RESPONSE")
        print("====================================")
        print(response.text)

        if response.status_code != 200:
            print(f"❌ API FAILED STATUS: {response.status_code}")
            return {}

        data = response.json()

        # =====================================================
        # NORMALIZE RESPONSE
        # =====================================================
        grouped_data = {}

        if isinstance(data, dict):

            if all(isinstance(v, list) for v in data.values()):
                grouped_data = {
                    str(k).strip().lower(): v
                    for k, v in data.items()
                }
            else:
                grouped_data = data

        elif isinstance(data, list):
            grouped_data = {"result": data}

        print("====================================")
        print("✅ ADDRESS API NORMALIZED OUTPUT")
        print("====================================")

        return grouped_data

    except Exception as e:
        print("❌ ADDRESS API ERROR:", e)
        return {}