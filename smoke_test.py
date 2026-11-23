"""Run with: python smoke_test.py (after pip install -r requirements.txt)."""
import io
import os
import tempfile

with tempfile.TemporaryDirectory(prefix='receiptdesk-test-') as data_dir:
    os.environ['RECEIPT_DATA_DIR'] = data_dir
    import app as module
    module.app.config['TESTING'] = True
    employee = module.app.test_client()
    manager = module.app.test_client()

    def csrf(client):
        with client.session_transaction() as session:
            return session['csrf']

    def login(client, email, password):
        client.get('/login')
        assert client.post('/login', data=dict(csrf=csrf(client), email=email, password=password)).status_code == 302

    login(employee, 'employee@example.com', os.getenv('EMPLOYEE_PASSWORD', 'employee-demo'))
    assert employee.post('/upload').status_code == 400
    response = employee.post('/upload', data={
        'csrf': csrf(employee), 'receipt': (io.BytesIO(b'%PDF-1.4\nDemo fixture'), 'receipt.pdf')
    }, content_type='multipart/form-data')
    assert response.status_code == 302
    module.executor.shutdown(wait=True)
    with module.connect() as db:
        receipt = db.execute('SELECT * FROM receipts').fetchone()
        assert receipt['status'] == 'Review'
        receipt_id = receipt['id']
    url = f'/receipts/{receipt_id}'
    assert employee.get(url).status_code == 200
    payload = dict(csrf=csrf(employee), merchant='Coffee Shop', purchase_date='2026-01-15',
                   total='12.50', currency='USD', tax='1.00', line_items='[]', action='submit')
    assert employee.post(url, data=payload).status_code == 302
    assert employee.post(url, data=payload).status_code == 403
    login(manager, 'manager@example.com', os.getenv('MANAGER_PASSWORD', 'manager-demo'))
    decision_url = url + '/decision'
    assert manager.post(decision_url, data=dict(csrf=csrf(manager), action='reject', comment='')).status_code == 302
    with module.connect() as db:
        assert db.execute('SELECT status FROM receipts WHERE id=?', (receipt_id,)).fetchone()[0] == 'Submitted'
    assert manager.post(decision_url, data=dict(csrf=csrf(manager), action='reject', comment='Please correct the amount.')).status_code == 302
    assert employee.post(url, data=payload).status_code == 302
    assert manager.post(decision_url, data=dict(csrf=csrf(manager), action='approve', comment='Verified.')).status_code == 302
    assert manager.post(decision_url, data=dict(csrf=csrf(manager), action='approve')).status_code == 409
    assert employee.post(url, data=payload).status_code == 403
    assert employee.get(url + '/file').status_code == 200
    assert manager.get('/').status_code == 200
    anonymous = module.app.test_client()
    assert anonymous.get(url + '/file').status_code == 302
    with module.connect() as db:
        assert db.execute('SELECT status FROM receipts WHERE id=?', (receipt_id,)).fetchone()[0] == 'Approved'
    print('PASS: upload, extraction, submission, mandatory rejection comment, resubmission, approval, locks, and authentication.')
