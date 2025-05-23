from flask import Flask, request, render_template_string
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError  # import specific error
from datetime import datetime
import os
import tempfile

app = Flask(__name__)

HTML_FORM = """
<!doctype html>
<title>NDR XML Validator</title>
<h2>NDR XML File Validator (XML file required not zip)</h2>
<h3>Validation Rules Guide</h3>
<p>Please ensure your XML file meets the following rules before uploading:</p>
<ul>
  <li>The XML file must be well-formed and valid.</li>
  <li>Each encounter must have a VisitDate and ARV regimen specified.</li>
  <li>ART start date should not be after any encounter visit dates.</li>
  <li>If TB positive status is present, IPT regimen must be included.</li>
  <li>Lab reports must include test ID and collection date.</li>
  <li>Regimens with duration greater than 30 days must have MultiMonthDispensing (MMD) specified.</li>
  <li>ARV codes in encounters must match the prescribed regimen codes.</li>
  <li>Patient age reported must closely match date of birth and report date.</li>
  <li>File must have the <code>.xml</code> extension.</li>
</ul>
<form method="post" enctype="multipart/form-data">
  <input type="file" name="file" accept=".xml" required>
  <input type="submit" value="Upload">
</form>

{% if error_message %}
  <p style="color:red;"><strong>{{ error_message }}</strong></p>
{% endif %}

{% if issues is not none %}
  <h3>Validation Report</h3>
  <ul>
  {% for issue in issues %}
    <li>{{ issue }}</li>
  {% endfor %}
  </ul>
{% endif %}
"""

def extract_services_with_dates(xml_file):
    tree = ET.parse(xml_file)
    root = tree.getroot()

    services = {
        'encounters': {},
        'regimens': {},
        'labs': {},
        'patient': {},
        'art_start': None,
        'ipt_codes': set()
    }

    patient = root.find('.//PatientDemographics')
    if patient is not None:
        services['patient']['dob'] = patient.findtext('PatientDateOfBirth')

    common = root.find('.//CommonQuestions')
    if common is not None:
        services['patient']['age'] = common.findtext('PatientAge')
        services['patient']['report_date'] = common.findtext('DateOfLastReport')

    hiv_q = root.find('.//HIVQuestions')
    if hiv_q is not None:
        art_date = hiv_q.findtext('ARTStartDate')
        if art_date:
            try:
                services['art_start'] = datetime.strptime(art_date, "%Y-%m-%d")
            except:
                pass

    for reg in root.findall('.//Regimen'):
        date = reg.findtext('VisitDate') or 'Unknown'
        code = reg.findtext('PrescribedRegimen/Code') or ''
        type_code = reg.findtext('PrescribedRegimenTypeCode')
        mmd = reg.findtext('MultiMonthDispensing')
        dsd = reg.findtext('DifferentiatedServiceDelivery')
        duration = reg.findtext('PrescribedRegimenDuration')
        services['regimens'][date] = {'code': code, 'type': type_code, 'mmd': mmd, 'dsd': dsd, 'duration': duration}
        if 'INH' in code.upper():
            services['ipt_codes'].add(date)

    for e in root.findall('.//HIVEncounter'):
        date = e.findtext('VisitDate') or 'Unknown'
        arv_code = e.findtext('ARVDrugRegimen/Code')
        tb = e.findtext('TBStatus')
        services['encounters'][date] = {'arv': arv_code, 'tb': tb}

    for l in root.findall('.//LaboratoryReport'):
        date = l.findtext('VisitDate') or 'Unknown'
        services['labs'][date] = {
            'test_id': l.findtext('LaboratoryTestIdentifier'),
            'collected': l.findtext('CollectionDate')
        }

    return services

def validate_ndr(services):
    issues = []
    for date, e in services['encounters'].items():
        if not e.get('arv'):
            issues.append(f"❌ Missing ARV regimen in encounter on {date}.")
        if date == 'Unknown':
            issues.append(f"❌ Missing VisitDate in an encounter.")

    art_start = services['art_start']
    if art_start:
        for date in services['encounters']:
            try:
                visit_date = datetime.strptime(date, "%Y-%m-%d")
                if visit_date < art_start:
                    issues.append(f"❌ Encounter on {date} before ARTStartDate {art_start.date()}.")
            except:
                pass

    tb_encounters = [d for d, e in services['encounters'].items() if e.get('tb') == '1']
    if tb_encounters and not services['ipt_codes']:
        for d in tb_encounters:
            issues.append(f"❌ TB positive on {d}, but no IPT regimen found.")

    for date, l in services['labs'].items():
        if not l.get('test_id') or not l.get('collected'):
            issues.append(f"❌ Lab report on {date} missing test ID or collection date.")

    for date, r in services['regimens'].items():
        try:
            duration = int(r.get('duration') or 0)
            if duration > 30 and not r.get('mmd'):
                issues.append(f"❌ Regimen on {date} has duration >30 but no MMD.")
        except:
            pass

    for date, e in services['encounters'].items():
        encounter_arv = e.get('arv')
        regimen = services['regimens'].get(date)
        if regimen and regimen.get('type') == 'ART':
            regimen_code = regimen.get('code')
            if encounter_arv and regimen_code and encounter_arv != regimen_code:
                issues.append(f"❌ ARV mismatch on {date}: Encounter={encounter_arv}, Regimen={regimen_code}")

    dob = services['patient'].get('dob')
    age = services['patient'].get('age')
    report_date = services['patient'].get('report_date')
    try:
        dob_dt = datetime.strptime(dob, "%Y-%m-%d")
        rpt_dt = datetime.strptime(report_date, "%Y-%m-%d")
        calc_age = rpt_dt.year - dob_dt.year - ((rpt_dt.month, rpt_dt.day) < (dob_dt.month, dob_dt.day))
        if abs(int(age) - calc_age) > 1:
            issues.append(f"❌ Reported age {age} does not match calculated age {calc_age}.")
    except:
        issues.append("⚠️ Could not validate DateOfBirth or age due to format issues.")

    return issues

@app.route('/', methods=['GET', 'POST'])
def upload_file():
    issues = None
    error_message = None  # <-- Add this to hold error messages

    if request.method == 'POST':
        file = request.files.get('file')
        if file and file.filename:
            # Check file extension server-side
            if not file.filename.lower().endswith('.xml'):
                error_message = "❌ Invalid file type. Please upload an XML file."
            else:
                temp_dir = tempfile.gettempdir()
                filepath = os.path.join(temp_dir, file.filename)

                file.save(filepath)

                try:
                    # Wrap parsing in try-except to catch malformed XML errors
                    services = extract_services_with_dates(filepath)
                    issues = validate_ndr(services)
                except ParseError:
                    error_message = "❌ Failed to parse XML. Please upload a well-formed XML file."
                except Exception as e:
                    # Optional: catch other exceptions and show generic error
                    error_message = f"❌ An unexpected error occurred: {str(e)}"
                finally:
                    if os.path.exists(filepath):
                        os.remove(filepath)
        else:
            error_message = "❌ No file uploaded."

    # Pass error_message to the template for display
    return render_template_string(HTML_FORM, issues=issues, error_message=error_message)



if __name__ == '__main__':
    app.run(debug=True)
