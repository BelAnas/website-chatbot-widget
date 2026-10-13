"""Writes the demo website into demo_site/.

The site belongs to Oakline Office, a fictional office-furniture supplier. Every page is
wrapped in a realistic layout (menu, cookie banner, footer, inline script) on purpose: the
crawler has to prove it can separate the content from that noise.

Run:  python -m scripts.build_demo_site
"""
from __future__ import annotations

import html
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "demo_site"

# slug -> (menu label, page title, intro paragraph, [(section heading, [paragraphs])])
PAGES: dict[str, tuple[str, str, str, list[tuple[str, list[str]]]]] = {
    "index": ("Home", "Oakline Office: furniture for working teams",
        "Oakline Office supplies desks, chairs and storage to companies, schools and co-working spaces across the continental United States.",
        [
            ("What we sell", [
                "Our range covers height-adjustable standing desks, ergonomic task chairs, lockers, pedestals and acoustic desk screens.",
                "Everything is sold per item on this website and at volume prices for teams of ten or more.",
            ]),
            ("Why companies choose Oakline", [
                "We have equipped more than 3,200 offices since 2014.",
                "Desk frames carry a 10-year warranty, standard delivery is free on orders over $500, and a space planner will draw your floor layout at no charge.",
            ]),
            ("How to order", [
                "Order online with a card, or ask for a quote if you are buying for a team.",
                "Approved business accounts can pay by purchase order with an invoice due in 30 days.",
            ]),
        ]),
    "standing-desks": ("Standing desks", "Standing desks",
        "Three electric height-adjustable desks, all with a steel frame and a choice of desktop finish.",
        [
            ("Lift One", [
                "The Lift One is our entry desk and costs $489. It has a single motor, a 120 x 60 cm desktop and lifts up to 70 kg.",
                "Its height adjusts from 72 to 118 cm with an up and down button.",
            ]),
            ("Lift Pro", [
                "The Lift Pro costs $689. It has two motors, a 160 x 80 cm desktop and lifts up to 120 kg.",
                "Its height adjusts from 62 to 127 cm, and the control panel stores four memory presets so each person can save a sitting and a standing height.",
                "A cable management tray is included with every Lift Pro.",
            ]),
            ("Lift Bench", [
                "The Lift Bench is a two-person desk for open-plan floors and costs $1,190.",
                "Each side moves on its own, so two colleagues can sit and stand independently.",
            ]),
            ("Desktop finishes", [
                "Desktops come in oak veneer, walnut veneer or white laminate at the same price.",
                "The laminate top is the most scratch resistant and is the one we recommend for shared desks.",
            ]),
            ("Power and noise", [
                "The desks plug into a standard wall outlet and use 0.1 W on standby.",
                "The motors run at under 45 decibels, which is quieter than a normal conversation.",
            ]),
        ]),
    "chairs": ("Chairs", "Office chairs",
        "Task chairs built for a full working day, with parts you can replace instead of replacing the chair.",
        [
            ("Forma task chair", [
                "The Forma costs $329. It has a breathable mesh back, adjustable lumbar support and a seat that slides to fit different leg lengths.",
                "It is rated for users up to 125 kg.",
            ]),
            ("Forma Plus", [
                "The Forma Plus costs $449 and adds a headrest and armrests that adjust in four directions.",
                "It is rated for users up to 150 kg and is the model we suggest for people taller than 190 cm.",
            ]),
            ("Perch stool", [
                "The Perch is a height-adjustable stool for standing desks and costs $159.",
                "Its rounded base lets you lean and move while you work.",
            ]),
            ("Fabrics and colours", [
                "Seats are available in charcoal, navy, forest green and sand.",
                "All seat fabrics are rated for 100,000 rub cycles, the standard for heavy commercial use.",
            ]),
            ("Replacement parts", [
                "Castors, gas lifts, armrest pads and seat covers are sold separately, from $9.",
                "Most parts can be swapped in a few minutes without tools.",
            ]),
        ]),
    "storage": ("Storage and screens", "Storage and acoustic screens",
        "Lockable storage and sound-absorbing screens that match the desk range.",
        [
            ("Stow pedestal", [
                "The Stow pedestal costs $149. It has three drawers, a central lock and fits under every Lift desk.",
            ]),
            ("Stow locker bank", [
                "A bank of four lockers costs $399. Each door has a combination lock, so there are no keys to lose.",
                "Banks can be bolted together side by side or stacked two high.",
            ]),
            ("Shelf wall", [
                "The Shelf wall is an open bookcase, 180 cm tall with five shelves, and costs $219.",
            ]),
            ("Acoustic desk screens", [
                "Desk screens cost $89 each and clamp onto any desktop between 18 and 40 mm thick.",
                "They are made from recycled felt and reduce speech noise between neighbouring desks.",
            ]),
        ]),
    "bulk-orders": ("Bulk orders", "Bulk orders and volume pricing",
        "Buying for a team? Prices drop as the order grows, and you get one person to deal with from quote to installation.",
        [
            ("Volume discounts", [
                "Orders of 10 to 24 items get 8% off the listed prices. Orders of 25 to 49 items get 12% off, and orders of 50 items or more get 15% off.",
                "For 100 items or more we prepare a custom quote.",
            ]),
            ("Getting a quote", [
                "Tell us what you need and where it is going, and we send a written quote within one business day.",
                "A quote is valid for 30 days and fixes the price even if our list prices change.",
            ]),
            ("Your account manager", [
                "Every order of 25 items or more is handled by a named account manager, who coordinates delivery dates, installation and invoicing.",
            ]),
            ("Try before you buy", [
                "Companies planning to order 10 chairs or more can borrow a sample chair for 14 days, free of charge.",
                "We deliver and collect the sample ourselves.",
            ]),
        ]),
    "delivery": ("Delivery", "Delivery",
        "We deliver to business and home addresses in the continental United States.",
        [
            ("Standard delivery", [
                "Standard delivery is free on orders over $500. Below that amount it costs $39.",
                "Standard orders arrive in 5 to 7 business days.",
            ]),
            ("Express delivery", [
                "Express delivery costs $79 and arrives in 2 to 3 business days.",
                "Express is available for in-stock items only and not for custom desktop sizes.",
            ]),
            ("Large orders", [
                "Orders of 50 items or more travel on pallets and are scheduled with you, usually 2 to 3 weeks after the order is confirmed.",
                "The driver needs a loading dock or ground-level access.",
            ]),
            ("Room of choice", [
                "For an extra $49 per order, the carrier brings the boxes into the room you choose instead of leaving them at the entrance.",
            ]),
            ("Tracking and delivery days", [
                "You receive a tracking link by email as soon as the order leaves our warehouse.",
                "Deliveries are made Monday to Friday. We do not deliver on Saturdays or Sundays.",
            ]),
        ]),
    "assembly": ("Assembly", "Assembly and installation",
        "Build it yourself in minutes, or let our crew install the whole floor.",
        [
            ("Self assembly", [
                "A desk takes about 25 minutes to assemble, and the only tool needed is included in the box.",
                "Chairs click together in about 5 minutes without tools.",
            ]),
            ("Installation service", [
                "Our installers assemble and place furniture for $35 per desk and $15 per chair.",
                "Installation is free on orders of 25 workstations or more.",
                "The crew takes away all packaging when the job is finished.",
            ]),
            ("Old furniture removal", [
                "We can remove your old desks and chairs for $20 per item.",
                "Removed furniture is donated when it is usable and recycled when it is not.",
            ]),
        ]),
    "returns": ("Returns", "Returns and refunds",
        "If something is not right, send it back.",
        [
            ("30-day returns", [
                "You can return any standard item within 30 days of delivery. The original packaging is not required.",
            ]),
            ("Return costs", [
                "If an item is faulty or arrived damaged, the return is free.",
                "For any other reason we charge a pickup fee of $25 per item.",
            ]),
            ("Refunds", [
                "We refund the original payment method within 5 business days of inspecting the returned item.",
            ]),
            ("What cannot be returned", [
                "Desktops cut to a custom size cannot be returned unless they are faulty.",
                "On orders of more than 50 items, returned goods carry a 15% restocking fee.",
            ]),
        ]),
    "warranty": ("Warranty", "Warranty",
        "Our furniture is built to stay in service, and the warranty says so in writing.",
        [
            ("What is covered and for how long", [
                "Desk frames are covered for 10 years. Motors and electronics are covered for 5 years.",
                "Chairs are covered for 7 years and seat fabrics for 2 years.",
            ]),
            ("Making a claim", [
                "Email a photo of the problem and your order number to support@oakline.example.",
                "We ship the replacement part within 3 business days, and you never have to send the whole desk back.",
            ]),
            ("What is not covered", [
                "The warranty does not cover normal wear, damage from misuse or changes made to the product.",
                "Furniture used around the clock in three-shift workplaces is covered for 3 years.",
            ]),
        ]),
    "space-planning": ("Space planning", "Free space planning",
        "Not sure how many desks fit? Our planners turn a floor plan into a furniture layout.",
        [
            ("How it works", [
                "Send us your floor plan as a PDF or a photo with measurements. We return a 2D layout within 3 business days.",
                "The service is free and comes with no obligation to order.",
            ]),
            ("3D renders", [
                "For projects of 20 workstations or more we also produce a 3D render, so you can see the room before you order.",
            ]),
            ("On-site measuring", [
                "If you have no floor plan, a planner can visit and measure the space, provided it is within 100 miles of Austin, Texas.",
            ]),
        ]),
    "showroom": ("Showroom", "Visit the showroom",
        "Sit in the chairs and try the desks before you decide.",
        [
            ("Address and opening hours", [
                "The showroom is at 410 Cedar Row, Austin, Texas.",
                "It is open Monday to Friday from 9 am to 6 pm and Saturday from 10 am to 4 pm. It is closed on Sundays.",
            ]),
            ("Appointments", [
                "You can walk in at any time during opening hours. Teams of four or more are asked to book an appointment so that a consultant is free for them.",
                "Parking in front of the building is free.",
            ]),
            ("Virtual tour", [
                "Too far to travel? Book a 30-minute video call and a consultant will walk you through the range on camera.",
            ]),
        ]),
    "sustainability": ("About", "About Oakline and sustainability",
        "Oakline Office was founded in Austin in 2014 and today has a team of 48 people.",
        [
            ("Materials", [
                "All wood in our desktops is FSC certified, and desk frames contain 62% recycled steel.",
                "Packaging is plastic-free and made from recycled cardboard.",
            ]),
            ("Take-back programme", [
                "When Oakline furniture reaches the end of its life, we collect it free of charge and recycle the materials.",
            ]),
            ("Repair before replace", [
                "We stock spare parts for every product for at least 10 years after it leaves the range.",
            ]),
        ]),
    "faq": ("FAQ", "Frequently asked questions",
        "Short answers to the questions we hear most.",
        [
            ("Which payment methods do you accept?", [
                "We accept Visa, Mastercard and American Express, and bank transfer for orders over $1,000.",
                "Approved business accounts can pay by purchase order with an invoice due in 30 days.",
            ]),
            ("Do you ship outside the United States?", [
                "We ship to Canada, with delivery from $120 per order. We do not ship to any other country at the moment.",
            ]),
            ("Can I order a custom desktop size?", [
                "Yes. Desktops can be cut to any width between 100 and 200 cm.",
                "A custom size adds $60 to the price and 10 business days to the delivery time.",
            ]),
            ("Can I change or cancel an order?", [
                "You can change or cancel an order free of charge within 24 hours of placing it.",
                "After that the order is usually packed, and the normal returns policy applies.",
            ]),
            ("Do you sell to schools and public bodies?", [
                "Yes. Schools, universities and public bodies get the volume discounts from the first item and can order tax-exempt by sending their exemption certificate.",
            ]),
        ]),
    "contact": ("Contact", "Contact us",
        "Talk to a person: we answer every message.",
        [
            ("Sales", [
                "For quotes and product advice, email sales@oakline.example or call (512) 555-0142.",
                "The sales team is available Monday to Friday from 8 am to 6 pm Central Time.",
            ]),
            ("Customer support", [
                "For help with an existing order, email support@oakline.example with your order number.",
                "We reply within 4 business hours.",
            ]),
        ]),
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(slug: str) -> str:
    _, title, intro, sections = PAGES[slug]
    nav = "".join(f'<li><a href="{s}.html">{html.escape(p[0])}</a></li>' for s, p in PAGES.items())
    body = "".join(
        f'<section><h2 id="{slugify(h)}">{html.escape(h)}</h2>'
        + "".join(f"<p>{html.escape(p)}</p>" for p in paras) + "</section>"
        for h, paras in sections)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} | Oakline Office</title>
<meta name="description" content="{html.escape(intro)}">
<link rel="stylesheet" href="site.css">
<script>window.analyticsQueue = window.analyticsQueue || []; analyticsQueue.push(["pageview", "{slug}"]);</script>
</head>
<body>
<header class="top">
  <a class="brand" href="index.html">Oakline Office</a>
  <nav aria-label="Main"><ul>{nav}</ul></nav>
</header>
<div class="cookie" role="note">We use cookies to remember your basket. <button type="button">Accept</button></div>
<main>
<h1>{html.escape(title)}</h1>
<p class="intro">{html.escape(intro)}</p>
{body}
</main>
<aside class="promo">Newsletter: get our office design guide. <form><input type="email" placeholder="Email"><button>Subscribe</button></form></aside>
<footer>
  <p>&copy; 2026 Oakline Office, a fictional company used for a software demo.</p>
  <ul><li><a href="contact.html">Contact</a></li><li><a href="returns.html">Returns</a></li><li><a href="warranty.html">Warranty</a></li></ul>
</footer>
<!-- The one line a site owner adds. The widget finds the service from its own address. -->
<script src="/widget.js" data-title="Oakline assistant" data-color="#1f5f4a" defer></script>
</body>
</html>
"""


CSS = """:root{--ink:#1c2321;--muted:#5d6b66;--line:#dfe5e1;--brand:#1f5f4a;--bg:#f7f8f6}
*{box-sizing:border-box}body{margin:0;font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
.top{display:flex;gap:24px;align-items:center;flex-wrap:wrap;padding:14px 24px;background:#fff;border-bottom:1px solid var(--line)}
.brand{font-weight:700;font-size:20px;color:var(--brand);text-decoration:none}
nav ul{display:flex;flex-wrap:wrap;gap:4px 16px;list-style:none;margin:0;padding:0}nav a{color:var(--muted);text-decoration:none;font-size:14px}nav a:hover{color:var(--brand)}
.cookie{background:#eef3f0;color:var(--muted);font-size:13px;padding:6px 24px}.cookie button{margin-left:8px}
main{max-width:760px;margin:0 auto;padding:32px 24px 80px}h1{font-size:34px;line-height:1.2;margin:0 0 8px}.intro{font-size:18px;color:var(--muted)}
section{background:#fff;border:1px solid var(--line);border-radius:10px;padding:4px 20px;margin:16px 0}h2{font-size:20px;margin:14px 0 4px}
.promo,footer{max-width:760px;margin:0 auto;padding:16px 24px;color:var(--muted);font-size:14px}footer ul{display:flex;gap:16px;list-style:none;padding:0}
"""


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for slug in PAGES:
        (OUT / f"{slug}.html").write_text(render(slug), encoding="utf-8")
    (OUT / "site.css").write_text(CSS, encoding="utf-8")
    # {base} is replaced with the real address when the service serves these two files.
    urls = "".join(f"<url><loc>{{base}}/{slug}.html</loc></url>" for slug in PAGES)
    (OUT / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        encoding="utf-8")
    (OUT / "robots.txt").write_text("User-agent: *\nDisallow: /basket\nSitemap: {base}/sitemap.xml\n", encoding="utf-8")
    words = sum(len(" ".join(p).split()) for _, _, _, secs in PAGES.values() for _, p in secs)
    print(f"wrote {len(PAGES)} pages, {sum(len(p[3]) for p in PAGES.values())} sections, about {words} words to {OUT}")


if __name__ == "__main__":
    main()
