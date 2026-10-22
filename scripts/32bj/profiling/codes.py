import sys, zipfile, ijson, csv
z = zipfile.ZipFile(sys.argv[1]); out = open(sys.argv[2], "w", newline=""); w = csv.writer(out)
with z.open(z.infolist()[0]) as f:
    for item in ijson.items(f, "in_network.item"):
        for nr in item["negotiated_rates"]:
            for pr in nr["negotiated_prices"]:
                w.writerow([item["billing_code_type"], item["billing_code"], pr["billing_class"], float(pr["negotiated_rate"]), "|".join(pr.get("billing_code_modifier") or [])])
