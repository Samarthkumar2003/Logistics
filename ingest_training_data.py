"""
ingest_training_data.py
-----------------------
Seeds the email_training_data table in Supabase with real examples
extracted from screenshots provided by Dhaval Shah (Bhatia Shipping).

Run ONCE after setup_rag.sql has been applied:
    python ingest_training_data.py

Add more examples to TRAINING_EXAMPLES below and re-run anytime.
Duplicates are avoided via upsert on (content hash).
"""

import hashlib
import logging
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

openai_client = OpenAI()
supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

# ──────────────────────────────────────────────────────────────────────────────
# TRAINING EXAMPLES
# Add new examples here — just append to the list.
# Each dict: subject, sender, body, label
# ──────────────────────────────────────────────────────────────────────────────

TRAINING_EXAMPLES: list[dict] = [

    # ── CUSTOMER REQUESTS ─────────────────────────────────────────────────────

    {
        "label": "customer_requirement",
        "subject": "ESR 017- SHARJAH TO INDIA -AIR FREIGHT/AIR COURIER CHARGES- 20/04/2026// CKL/SR/26/8459",
        "sender": "Akhila Yesudas - CSS Kingston <pricing1@csskingston.com>",
        "body": (
            "Dear team,\n\n"
            "Kindly note that we have air import shipment to Bangalore. "
            "Kindly advise the import clearances & delivery charges asap.\n\n"
            "Delivery address:\n"
            "ESPA WATER SYSTEMS PVT LTD\n"
            "No. 3/1, Plot No. 3, Puttapa Industrial Estate, Whitefield Road\n"
            "Mahadevapura.P.O, Bangalore – 560048, India\n\n"
            "Number of Pallet: 1\n"
            "Dimensions: L 132CM X W83CM X H100CM\n"
            "TTL Gross Weight: 375 KGS\n"
            "Description of goods: Water pump and Accessories"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "EXW CHARGES // INDIA TO UAE // 1 X20 FT CONTAINER",
        "sender": "SANTHOSH-SALES DUBAI CLPL <sales2.aeron@caravellogistics.com>",
        "body": (
            "Hi,\n\n"
            "Please provide your best rate for the below EXW shipment from INDIA TO UAE.\n\n"
            "POL: KHORFAKKAN\n"
            "POD: 1X20FT NON HAZ CONTR\n"
            "TERM: EX WORK\n"
            "CARGO READINESS: 24 APR\n\n"
            "PICK UP ADDRESS:\n"
            "VESUVIUS INDIA LIMITED\n"
            "212B, G.I.D.C ESTATE PHASE-1\n"
            "MEHSANA - 384002, GUJARAT\n\n"
            "Thanks & Regards,\n"
            "Santhosh R.\n"
            "Caravel Logistics (M.E.) LLC (As Agents)"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "FOB MUNDRA / JEDDAH 16 X 20'GP",
        "sender": "Munteha Ali Khan <Munteha.k@fracht-me.com>",
        "body": (
            "Dear Partner,\n\n"
            "Please quote your best price for below enquiry.\n\n"
            "FOB MUNDRA / JEDDAH 16 X 20' STD\n"
            "COMMODITY FOOD STUFF READY TO LOAD\n"
            "NORMAL WEIGHT\n\n"
            "With Best Regards,\n"
            "Munteha Ali Khan\n"
            "Sales Manager, Riyadh (CP)\n"
            "Fracht GROUP Saudi Arabia"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "INDIA TO SAUDI ARABIA SEA FREIGHT ENQUIRY",
        "sender": "ALS LINE – Alliance Lines",
        "body": (
            "Dear Dhaval,\n\n"
            "Greetings from ALS LINE – Alliance Lines!\n\n"
            "We have below shipment coming from (INDIA) to Saudi Arabia. "
            "Due to current situation give us all options based on the carrier acceptance. "
            "Please check and let us know 2-3 options, including a reliable carrier and cheapest carrier. "
            "So that we can give the options to Cnee and move the shipment with their preferred options.\n\n"
            "MODE: SEA/FCL/LCL 20FT\n"
            "TERM: EX WORKS\n"
            "AOD: JEDDAH\n\n"
            "COLLECTION ADDRESS:\n"
            "SF NO 252/1B1, Bodipalayam,\n"
            "Seerapalayam Village, Madukkarai Post,\n"
            "Coimbatore-641105, INDIA\n\n"
            "PACKING DETAILS:\n"
            "11 boxes, dimensions 122x82x84 cm and 584x26x26 cm and 127x77x39 cm\n"
            "Total approx weight: 1,819 KGS"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "MUNDRA TO JEBEL ALI KSJDXB9335",
        "sender": "Anagha Ganesh <Anagha.ganesh@ksjship.ae>",
        "body": (
            "Dear Dhaval,\n\n"
            "Please quote for below.\n\n"
            "POL: MUNDRA\n"
            "POD: JEBEL ALI\n"
            "Equipment: 20/40FT\n\n"
            "Best Regards,\n"
            "Anagha Ganesh\n"
            "KSJ Ship"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "FREIGHT ENQUIRY INDIA TO JEBEL ALI / KHORFAKKAN 1X20FT",
        "sender": "Arunima Menon <arunima@jctrans.ae>",
        "body": (
            "Dear Dhaval,\n\n"
            "Please advise your best rate for the below enquiry:\n\n"
            "India to Jebel Ali / Khorfakkan\n"
            "1 X 20FT\n\n"
            "Please note the collection address as follows - Lucknow, India-226020\n\n"
            "Best Regards,\n"
            "Arunima Menon\n"
            "Business Development Executive\n"
            "Office: +971 6 550 9944"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "Rate ex Carbonaire to Tokyo Terminal via Sea",
        "sender": "Natasha Parsons <natasha@halifaxinternational.com.au>",
        "body": (
            "Good morning,\n\n"
            "Please advise rate for the below.\n\n"
            "PRODUCT DESCRIPTION: SIDE POST INSULATOR\n"
            "PRODUCT DRAWING NO.: 9492\n"
            "QTY PER BOX: 1600 NOS [1600 NOS X 1 BOX X 1 PLT]\n"
            "BOX SIZE WITH PALLET: 120cm X 80cm X 53cm\n"
            "NO. OF BOX: 1 BOX\n"
            "NO. OF PALLETS: 1 PLT\n"
            "TOTAL NETT WT.: 89.600 KGS\n"
            "TOTAL GROSS WT.: 117.600 KGS"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "RFQ India to United Gas Co LLC Sharjah////QAR260190",
        "sender": "Devadathan - CSS Kingston <devadathan@csskingston.com>",
        "body": (
            "Dear Team,\n\n"
            "Good day.\n\n"
            "Please find the attached Shipping list and send us your offer ocean freight offer "
            "with lead time from the following to UAE.\n\n"
            "We will require 2 x 40 FR OOG.\n\n"
            "Factory Address:\n"
            "S.No. 461/1A, 461/1B, No. 22, Kannankottai Road,\n"
            "Kannankottai, Nemallur Village, Gumidipoondi Taluk,\n"
            "Thiruvallur District, Tamilnadu-601 202.\n\n"
            "Please advise the rates with all the charges included!"
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "SOS/KAM/0074/20Apr // : FOB - Mundra -Jeddah,KSA",
        "sender": "Geena|Skyocean <csdxb6@skyoceanlogistics.net>",
        "body": (
            "Dear Team,\n\n"
            "Kindly assist with your best quotation for the below shipment details:\n\n"
            "Shipment Details:\n"
            "POL: Mundra\n"
            "POD: Jeddah\n"
            "Equipment: 1 x 20' Container\n"
            "Mode: FOB\n"
            "Commodity: Rice\n"
            "Customer: Aiswariya Foods\n\n"
            "Kindly provide:\n"
            "- Freight rate with full breakdown\n"
            "- Origin & destination local charges\n"
            "- Transit time & sailing details (ETD/ETA)\n"
            "- Free time\n"
            "- Validity of rates\n\n"
            "We look forward to your prompt response."
        ),
    },
    {
        "label": "customer_requirement",
        "subject": "RATE ENQUIRY POL COCHIN POD JEA 20GP X2 RICE",
        "sender": "Geena Sajeev <csdxb6@skyoceanlogistics.net>",
        "body": (
            "Dear Sir,\n\n"
            "Kindly Share the Rates.\n\n"
            "POL: Cochin\n"
            "POD: JEA\n"
            "Equipment: 20 GP X 2\n"
            "Terms: Ocean FOB\n"
            "Product: Rice\n"
            "CUSTOMER: KALAMENN FZC LLC\n\n"
            "Thanks & Regards,\n"
            "Geena Sajeev\n"
            "Senior Pricing Executive\n"
            "Sky Ocean Shipping"
        ),
    },

    # ── QUOTATION RATE CARDS ──────────────────────────────────────────────────

    {
        "label": "quotation_rate_card",
        "subject": "RATES: INDIA TO UAE – FCL/LCL – VALID APR 2026",
        "sender": "pricing@globalfreight.ae",
        "body": (
            "Dear Dhaval,\n\n"
            "Please find our updated rates for India to UAE below.\n\n"
            "POL: NHAVA SHEVA / MUNDRA\n"
            "POD: JEBEL ALI / KHORFAKKAN\n\n"
            "FCL RATES (USD per container):\n"
            "20GP: USD 450\n"
            "40GP: USD 750\n"
            "40HC: USD 780\n\n"
            "LCL RATE: USD 22 per CBM (min 1 CBM)\n\n"
            "TRANSIT TIME: 7-9 days via Jebel Ali\n"
            "FREE TIME: 14 days at destination\n"
            "VALIDITY: 30 Apr 2026\n\n"
            "Note: Rates exclude origin THC, BL fee, and documentation charges.\n\n"
            "Best Regards,\nPricing Team\nGlobal Freight LLC"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "Ocean Freight Rates – MUNDRA to JEDDAH – MSC/COSCO – Apr 2026",
        "sender": "rates@seabridge-logistics.com",
        "body": (
            "Hi Dhaval,\n\n"
            "Sharing our competitive rates for Mundra to Jeddah:\n\n"
            "CARRIER: MSC\n"
            "POL: MUNDRA\n"
            "POD: JEDDAH\n"
            "20GP: USD 850 all-in\n"
            "40GP: USD 1,200 all-in\n"
            "40HC: USD 1,250 all-in\n\n"
            "CARRIER: COSCO\n"
            "20GP: USD 780\n"
            "40GP: USD 1,100\n\n"
            "ETD: Every Tuesday & Friday\n"
            "Transit: 10 days\n"
            "Validity: 15 May 2026\n\n"
            "Above rates are subject to space and equipment availability.\n\n"
            "Regards,\nSeaBridge Logistics"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "RE: RFQ – FCL RATES INDIA/SAUDI – CMA CGM",
        "sender": "export.pricing@cmacgm-agent.in",
        "body": (
            "Dear Dhaval,\n\n"
            "Thank you for your enquiry. Please find our rates below.\n\n"
            "ORIGIN: NHAVA SHEVA\n"
            "DESTINATION: JEDDAH / DAMMAM / RIYADH\n\n"
            "20GP: USD 900\n"
            "40GP: USD 1,400\n"
            "40HC: USD 1,450\n"
            "40RF (Reefer): USD 2,800\n\n"
            "SURCHARGES (included in above):\n"
            "- BAF: USD 80/20GP\n"
            "- CAF: USD 30/20GP\n"
            "- ISPS: USD 10/BL\n\n"
            "FREE TIME: 14 days DEM + 14 days DET\n"
            "TRANSIT TIME: 14 days\n"
            "CARRIER: CMA CGM\n"
            "VALIDITY: 30 Apr 2026\n\n"
            "Please revert to confirm booking.\n\n"
            "Regards,\nExport Pricing\nCMA CGM India"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "SPOT RATE – COCHIN TO DUBAI – 20GP/40GP – EVERGREEN",
        "sender": "spotrates@transworld.ae",
        "body": (
            "Dear Sir,\n\n"
            "Spot rates for your reference:\n\n"
            "POL: COCHIN (INDIA)\n"
            "POD: JEBEL ALI (UAE)\n"
            "CARRIER: EVERGREEN\n\n"
            "20'GP : USD 380\n"
            "40'GP : USD 600\n"
            "40'HC : USD 620\n\n"
            "OFR includes: Ocean freight + BAF\n"
            "Excludes: THC origin/dest, B/L fee, customs\n\n"
            "SAILING: Weekly (every Wednesday)\n"
            "T/T: 6-7 days\n"
            "FREE TIME DEST: 14 days\n"
            "RATE VALIDITY: 30-Apr-2026\n\n"
            "Kindly confirm booking request at the earliest.\n\n"
            "Thanks & Regards,\nTransworld Shipping"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "LCL TARIFF – INDIA TO UAE/OMAN – Q2 2026",
        "sender": "lcl.pricing@consolidators.in",
        "body": (
            "Dear Dhaval,\n\n"
            "Please find our Q2 2026 LCL tariff for India to Gulf:\n\n"
            "ORIGIN: NHAVA SHEVA / MUNDRA / CHENNAI\n\n"
            "DESTINATION         RATE (USD/CBM)  MIN CHARGE\n"
            "Jebel Ali, UAE      USD 28/CBM     USD 85\n"
            "Sharjah, UAE        USD 30/CBM     USD 90\n"
            "Sohar, Oman         USD 35/CBM     USD 100\n"
            "Muscat, Oman        USD 38/CBM     USD 110\n\n"
            "FREQUENCY: Weekly consolidations every Monday\n"
            "TRANSIT TIME: 10-12 days\n"
            "CUT-OFF: 3 days prior sailing\n"
            "VALIDITY: 30 Jun 2026\n\n"
            "Above rates inclusive of BAF. Excludes origin stuffing.\n\n"
            "Best Regards,\nLCL Pricing Desk"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "AIR FREIGHT RATES – MUMBAI/DEL TO DXB – APR-MAY 2026",
        "sender": "airpricing@kargoway.com",
        "body": (
            "Dear Dhaval,\n\n"
            "Sharing our air freight rates for your reference.\n\n"
            "ORIGIN: BOM (Mumbai) / DEL (Delhi)\n"
            "DESTINATION: DXB (Dubai)\n\n"
            "GENERAL CARGO:\n"
            "BOM-DXB: USD 1.60/kg (min 45 kg)\n"
            "DEL-DXB: USD 1.45/kg (min 45 kg)\n\n"
            "HEAVY CARGO (>100 kg):\n"
            "BOM-DXB: USD 1.35/kg\n"
            "DEL-DXB: USD 1.25/kg\n\n"
            "CHARGEABLE WEIGHT: Actual or volumetric (1:6), whichever higher\n"
            "TRANSIT: 1-2 days\n"
            "AIRLINES: Emirates / Air Arabia / IndiGo (codeshare)\n"
            "VALIDITY: 31 May 2026\n\n"
            "Fuel surcharge included. Excludes pickup, customs, and delivery.\n\n"
            "Thanks,\nKargo Way Air Freight"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "FCL RATE UPDATE: NHAVA SHEVA → DAMMAM/JUBAIL – HAPAG-LLOYD",
        "sender": "dxb.pricing@interfreight.com",
        "body": (
            "Hi Dhaval,\n\n"
            "Rate update as requested:\n\n"
            "CARRIER: HAPAG-LLOYD\n"
            "POL: NHAVA SHEVA\n"
            "POD: DAMMAM / JUBAIL\n\n"
            "20'ST : USD 1,050 W/M\n"
            "40'ST : USD 1,600 W/M\n"
            "40'HC : USD 1,650 W/M\n\n"
            "INCLUDED: OFR + BAF + CAF\n"
            "EXCLUDED: Origin THC (INR 6,200/20GP), BL fee (USD 55), ISPS\n\n"
            "ETD: Bi-weekly (Tues/Sat ex NSPT)\n"
            "TRANSIT: 16–18 days\n"
            "FREE TIME: 14 DEM / 7 DET at POD\n"
            "VALIDITY: 15-May-2026\n\n"
            "Space is limited – please confirm ASAP.\n\n"
            "Best,\nInterFreight DXB Pricing"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "RATE OFFER – MUNDRA TO TOKYO/OSAKA – ONE LINE",
        "sender": "one.pricing@oceannetworkexpress.in",
        "body": (
            "Dear Dhaval,\n\n"
            "Please find our rate offer as below.\n\n"
            "CARRIER: ONE (Ocean Network Express)\n"
            "POL: MUNDRA\n"
            "POD: TOKYO / OSAKA / YOKOHAMA\n\n"
            "EQUIPMENT     OCEAN FREIGHT\n"
            "20GP          USD 680\n"
            "40GP          USD 1,050\n"
            "40HC          USD 1,080\n\n"
            "SURCHARGES:\n"
            "Peak Season Surcharge (PSS): USD 150/20GP, USD 250/40GP\n"
            "BAF: Included\n"
            "CAF: USD 40/20GP\n\n"
            "TRANSIT TIME: 18-21 days\n"
            "FREQUENCY: Every Friday ex Mundra\n"
            "FREE TIME: 7 days at POD\n"
            "VALIDITY: 30 Apr 2026\n\n"
            "Regards,\nONE India Pricing"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "REVISED RATES: NHAVA SHEVA/MUNDRA to VARIOUS GULF PORTS",
        "sender": "pricing@gulflinkslogistics.ae",
        "body": (
            "Dear Dhaval,\n\n"
            "Please find our revised rate card for April 2026:\n\n"
            "NHAVA SHEVA TO:\n"
            "Jebel Ali:     20GP $420 | 40GP $680 | 40HC $700\n"
            "Sharjah:       20GP $440 | 40GP $700 | 40HC $720\n"
            "Abu Dhabi:     20GP $460 | 40GP $720 | 40HC $740\n"
            "Sohar:         20GP $480 | 40GP $760 | 40HC $780\n\n"
            "MUNDRA TO:\n"
            "Jebel Ali:     20GP $400 | 40GP $650 | 40HC $670\n"
            "Khorfakkan:    20GP $390 | 40GP $640 | 40HC $660\n\n"
            "All rates USD per container, ocean freight only.\n"
            "BAF included. THC, BL, ISPS excluded.\n"
            "Validity: 30-Apr-2026. Subject to space & equipment.\n\n"
            "Regards,\nGulfLinks Logistics"
        ),
    },
    {
        "label": "quotation_rate_card",
        "subject": "RE: ENQUIRY – COCHIN TO JEDDAH 1x40HC – OUR BEST RATES",
        "sender": "cochin@bluewater-shipping.com",
        "body": (
            "Dear Dhaval,\n\n"
            "With reference to your enquiry, we are pleased to offer:\n\n"
            "ROUTE: COCHIN (INCOCHIN) → JEDDAH (SAJED)\n"
            "EQUIPMENT: 40'HC\n"
            "CARRIER: MSC / Maersk (subject to availability)\n\n"
            "OCEAN FREIGHT: USD 1,100 per 40HC\n"
            "BAF/CAF: Included\n"
            "PSS: USD 200 (Apr loading only)\n\n"
            "ORIGIN CHARGES (approx.):\n"
            "THC: INR 9,800\n"
            "BL Fee: USD 60\n"
            "ISPS: INR 1,500\n\n"
            "TRANSIT TIME: 9-11 days\n"
            "ETD: Weekly Tuesdays\n"
            "FREE TIME DEST: 14 days\n"
            "RATE VALIDITY: 10-May-2026\n\n"
            "Look forward to your booking confirmation.\n\n"
            "Warm Regards,\nBluewater Shipping – Cochin"
        ),
    },

    # ── WHATSAPP – CUSTOMER REQUESTS ─────────────────────────────────────────
    # Individual message bubbles extracted from WhatsApp screenshots.
    # No subject/sender — content field holds the raw bubble text.

    {
        "label": "customer_requirement",
        "source": "whatsapp",
        "subject": "[WhatsApp] Cocopeat 40ft Nhava Sheva to Gulf",
        "sender": "whatsapp_client",
        "body": (
            "Please send price for below:\n"
            "40ft\n"
            "Cocopeat blocks\n"
            "25-26 ton\n"
            "Nhava Sheva to Khor Fakkan / Jebel Ali / Abu Dhabi"
        ),
    },
    {
        "label": "customer_requirement",
        "source": "whatsapp",
        "subject": "[WhatsApp] Mundra to Aden 05x20 ceramic target rate",
        "sender": "whatsapp_client",
        "body": (
            "Mundra to Aden 05x20, ceramic.\n"
            "Target rate is usd1350/20 inclusive all DTHC, D/O + container cleaning "
            "are on client's account. Pls check CSL"
        ),
    },
    {
        "label": "customer_requirement",
        "source": "whatsapp",
        "subject": "[WhatsApp] Chennai to Khorfakkan 1x40RF food items",
        "sender": "whatsapp_client",
        "body": (
            "Chennai → Khorfakkan / 1x40RF\n"
            "cargo is food items"
        ),
    },
    {
        "label": "customer_requirement",
        "source": "whatsapp",
        "subject": "[WhatsApp] POL Chennai Tuticorin to Aqaba Doha Sohar granites",
        "sender": "whatsapp_client",
        "body": (
            "POL: Chennai, Tuticorin\n"
            "POD: Aqaba, Doha Sohar\n"
            "Commodity: Granites\n"
            "Weight: 28mts\n"
            "Requirement for 20ft: 7 containers"
        ),
    },
    {
        "label": "customer_requirement",
        "source": "whatsapp",
        "subject": "[WhatsApp] Mumbai Jebel Ali food stuff ready to load",
        "sender": "whatsapp_client",
        "body": (
            "MUMBAI / JEBEL ALI 5 LB\n"
            "COMMODITY FOOD STUFF READY TO LOAD"
        ),
    },

    # ── WHATSAPP – QUOTATION RATE CARDS ──────────────────────────────────────

    {
        "label": "quotation_rate_card",
        "source": "whatsapp",
        "subject": "[WhatsApp] MSC Nhava Sheva to Khorfakkan USD 3400 per 40ft",
        "sender": "whatsapp_agent",
        "body": (
            "POL: Nhava Sheva\n"
            "POD: Khorfakkan\n"
            "Carrier: MSC\n"
            "Ocean Freight from Nhava Sheva to Khorfakkan: USD 3400 per 40'\n"
            "EFS: USD 110 per container\n"
            "Haulage from Khorfakkan to Jebel Ali or Abu Dhabi: USD 1250 per 40'\n"
            "Vessel sailing 10th May\n"
            "T.T. 8 days\n"
            "Destination Free Time: 14\n"
            "plus locals at actuals"
        ),
    },
    {
        "label": "quotation_rate_card",
        "source": "whatsapp",
        "subject": "[WhatsApp] CMA CGM full charge breakdown India to Jebel Ali",
        "sender": "whatsapp_agent",
        "body": (
            "Cargo pick-up: USD 250 per 40'\n"
            "Customs Agency: USD 50 per 40' (considering the shipper has factory stuffing permission)\n"
            "Carrier: CMA CGM\n"
            "Ocean Freight: USD 325 per 40'\n"
            "THC: USD 200 per 40'\n"
            "ISPS: USD 14 per container\n"
            "MUC: USD 3 per container\n"
            "Seal charges: USD 10 per container\n"
            "Toll charges: USD 10 per teu\n"
            "IHC: USD 600 per 40'\n"
            "OTHC: USD 100 per 40'\n"
            "BL Fees: USD 50 per bl\n"
            "Validity: 30th November\n"
            "Destination Free Time: 14\n"
            "Cargo Pick-up: 15th November\n"
            "ETD Nhava Sheva: 26th November\n"
            "ETA Jebel Ali: 1st December"
        ),
    },
    {
        "label": "quotation_rate_card",
        "source": "whatsapp",
        "subject": "[WhatsApp] Nhava Sheva Aden 20ft USD 3300 carrier rate",
        "sender": "whatsapp_agent",
        "body": (
            "Nhava Sheva - Aden 20'\n"
            "USD 3300/-\n"
            "But vessel is directly in May"
        ),
    },
    {
        "label": "quotation_rate_card",
        "source": "whatsapp",
        "subject": "[WhatsApp] Chennai Khorfakkan 40RF USD 806 bonded trucking",
        "sender": "whatsapp_agent",
        "body": (
            "USD$806/40'RF + AED6502 (Bonded Trucking to AEJEA)"
        ),
    },
    {
        "label": "quotation_rate_card",
        "source": "whatsapp",
        "subject": "[WhatsApp] Aqaba Hamad Sohar 20ft spot rates",
        "sender": "whatsapp_agent",
        "body": (
            "AQB: $1506/20'\n"
            "HMD/ SOH: $856/20'"
        ),
    },

    # ── GENERAL ──────────────────────────────────────────────────────────────
    {
        "label": "general",
        "subject": "Weekly Logistics Industry Update",
        "sender": "newsletter@freightnews.com",
        "body": (
            "Top stories this week: Container shipping rates continue to stabilize. "
            "Port congestion easing at major Asian hubs. "
            "New sustainability regulations coming into effect Q3."
        ),
    },
    {
        "label": "general",
        "subject": "Invoice #INV-2026-0441 attached",
        "sender": "accounts@vendor.com",
        "body": (
            "Dear Sir/Madam,\n\n"
            "Please find attached invoice INV-2026-0441 for services rendered in April 2026.\n"
            "Payment due within 30 days.\n\n"
            "Regards,\nAccounts Team"
        ),
    },
    {
        "label": "general",
        "subject": "Shipment Tracking Update - AWB 123456789",
        "sender": "noreply@dhl.com",
        "body": (
            "Your shipment AWB 123456789 has been delivered.\n"
            "Delivered to: John Smith on 20 Apr 2026 at 14:32.\n"
            "Thank you for shipping with DHL."
        ),
    },
]


# ──────────────────────────────────────────────────────────────────────────────

def embed(text: str) -> list[float]:
    # Qwen document embedding (no instruction) — matches email_training_data.embedding_qwen
    from email_classifier import _get_embedding
    return _get_embedding(text)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _build_content(ex: dict) -> str:
    """Format content string differently for email vs whatsapp bubbles."""
    source = ex.get("source", "manual")
    if source == "whatsapp":
        return f"[WhatsApp message]\n{ex['body']}"
    return f"Subject: {ex['subject']}\nFrom: {ex['sender']}\n\n{ex['body']}"


def _fetch_existing_hashes() -> set[str]:
    """Fetch all existing content from DB, hash locally, return as a set."""
    rows = supabase.table("email_training_data").select("content").execute()
    return {content_hash(r["content"]) for r in rows.data if r.get("content")}


def ingest(examples: list[dict]) -> None:
    logger.info("Ingesting %d training examples...", len(examples))

    try:
        existing_hashes = _fetch_existing_hashes()
        logger.info("Existing hashes in DB: %d", len(existing_hashes))
    except Exception as e:
        logger.error("Cannot connect to Supabase: %s", e)
        sys.exit(1)

    counts: dict[str, int] = {
        "customer_requirement": 0,
        "quotation_rate_card": 0,
        "general": 0,
        "skipped": 0,
    }

    for i, ex in enumerate(examples, 1):
        source = ex.get("source", "manual")
        content = _build_content(ex)
        label = ex["label"]
        chash = content_hash(content)

        if chash in existing_hashes:
            counts["skipped"] += 1
            logger.info("[%2d/%d] SKIP (exists) — %s", i, len(examples), ex.get("subject", "")[:55])
            continue

        try:
            vec = embed(content)
            supabase.table("email_training_data").insert({
                "content": content,
                "subject": ex.get("subject", ""),
                "sender": ex.get("sender", ""),
                "label": label,
                "source": source,
                "embedding_qwen": vec,
            }).execute()
            existing_hashes.add(chash)   # prevent re-insert within same run
            counts[label] = counts.get(label, 0) + 1
            logger.info("[%2d/%d] ✓ [%s] %s — %s", i, len(examples), source, label, ex.get("subject", "")[:55])
        except Exception as e:
            logger.error("[%2d/%d] FAILED — %s: %s", i, len(examples), ex.get("subject", "")[:55], e)

    logger.info("\n=== Done ===")
    logger.info("inserted  customer_requirement : %d", counts["customer_requirement"])
    logger.info("inserted  quotation_rate_card  : %d", counts["quotation_rate_card"])
    logger.info("inserted  general              : %d", counts["general"])
    logger.info("skipped (already exist)        : %d", counts["skipped"])


if __name__ == "__main__":
    ingest(TRAINING_EXAMPLES)
