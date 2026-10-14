import csv
from datetime import date,timedelta
from fba_replenishment.core import connect,import_csv,stop,resume,calculate

def write(path,header,rows):
 with open(path,'w',newline='') as f:
  w=csv.writer(f);w.writerow(header);w.writerows(rows)

def test_family_stop(tmp_path):
 c=connect(tmp_path/'t.db')
 a=tmp_path/'sku.csv';write(a,['sku'],[['J001-size 5'],['J001-size 6'],['J002-size 5'],['UNMAPPED']]);import_csv(c,'sku',a,'US')
 b=tmp_path/'stock.csv';write(b,['sku','fulfillable'],[[x,5] for x in ['J001-size 5','J001-size 6','J002-size 5']]);import_csv(c,'stock',b,'US')
 d=tmp_path/'sales.csv';write(d,['sku','day','units'],[[x,(date.today()-timedelta(days=i)).isoformat(),1] for x in ['J001-size 5','J001-size 6','J002-size 5'] for i in range(1,31)]);import_csv(c,'sales',d,'US')
 stop(c,'US','J001-size 5','Discontinued')
 rows={r['sku']:r for r in calculate(c)}
 assert rows['J001-size 6']['status']=='STOPPED' and rows['J001-size 6']['recommended_qty']==0
 assert rows['J002-size 5']['status']=='ACTIVE' and rows['J002-size 5']['recommended_qty']>0
 assert rows['UNMAPPED']['status']=='FAMILY_REVIEW_REQUIRED'
 resume(c,'US','J001-size 5','manager')
 rows={r['sku']:r for r in calculate(c)}
 assert rows['J001-size 6']['status']=='ACTIVE'

def test_stop_persists_new_child(tmp_path):
 c=connect(tmp_path/'t.db');p=tmp_path/'a.csv'
 write(p,['sku'],[['J001-size 5']]);import_csv(c,'sku',p,'US');stop(c,'US','J001-size 5','Stop all')
 write(p,['sku'],[['J001-size 7']]);import_csv(c,'sku',p,'US')
 assert calculate(c)[1]['status']=='STOPPED'
