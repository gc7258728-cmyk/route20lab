# -*- coding: utf-8 -*-
"""Route20Lab : diagnostic pédagogique, exposition au trafic et entretien.

Les seuils sont des critères du projet, pas un catalogue de dimensionnement.
Les calculs ne prédisent pas une durée de vie mécanique de chaussée.
"""
from __future__ import annotations

import base64
import copy
from contextlib import closing
from datetime import date
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

VERSION = '1.0'
INK, YELLOW, TEAL, RED = '#172326', '#E9ED65', '#087F83', '#B44635'
LAYER_NAMES = ['Roulement', 'Base', 'Fondation', 'Forme']
MATERIALS = ['Béton bitumineux', 'Grave-bitume', 'Grave latéritique', 'Matériau de couche de forme']
LAYER_COLORS = ['#273538', '#56676A', '#B47E50', '#D8BE8D']
SOURCES = {
    'Trafic et essieux équivalents': 'https://construction-moderne.infociments.fr/route/dimensionnement-des-voiries-et-amenagements-urbains-en-beton?page=1',
    'Conception hydraulique des buses (FHWA)': 'https://www.fhwa.dot.gov/engineering/hydraulics/culverthyd/culvert.cfm',
    'Gestion du cycle de vie (PIARC)': 'https://road-asset.piarc.org/en/data-and-modeling-lifecycle-planning/lifecycle-planning',
    'Dépendances Streamlit Cloud': 'https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies',
}
MODES = ['Passages par jour', 'Un camion toutes les… minutes']
STATUS = {'ok': 'Critère satisfait', 'bad': 'Écart détecté', 'unknown': 'À renseigner'}
ICONS = {'ok': ':material/check_circle:', 'bad': ':material/error:', 'unknown': ':material/help:'}


def uid():
    return uuid.uuid4().hex[:12]


def numeric(x, label, lo=0., hi=1e12, integer=False):
    if isinstance(x, bool):
        raise ValueError(f'{label} : nombre attendu.')
    try:
        x = float(x)
    except (ValueError, TypeError):
        raise ValueError(f'{label} : champ numérique invalide.') from None
    if not math.isfinite(x) or not lo <= x <= hi or (integer and x != int(x)):
        raise ValueError(f'{label} : valeur attendue entre {lo:g} et {hi:g}' + (' (entier).' if integer else '.'))
    return int(x) if integer else x


def text(x, label, limit=500, required=False):
    if not isinstance(x, str) or len(x) > limit or (required and not x.strip()):
        raise ValueError(f'{label} : texte ' + ('obligatoire, ' if required else '') + f'{limit} caractères maximum.')
    return x.strip()


def flag(x, label):
    if type(x) is not bool:
        raise ValueError(f'{label} : indicateur invalide.')
    return x


def new_segment(start=0., end=1000., demo=False):
    return dict(id=uid(), name='Tronçon A', start=start, end=end, alignment='Alignement droit',
        traffic=dict(mode=MODES[1], daily=48., interval=30., hours=24., days=365, growth=0., model='Sans croissance',
            share=100., mass=12.5, cam=0., source='Comptage fictif : fréquence 30 min, 24 h/24, tous passages sur la voie étudiée.' if demo else ''),
        soil=[dict(pk=(start+end)/2, cbr=18., condition='Après immersion', date='2026-10-01', source='Essai fictif') ] if demo else [],
        water=dict(cross_left=2.5,cross_right=2.5,cant=4.,shoulder_left=3.5,shoulder_right=3.5,
            shape='Trapézoïdal',bottom=.5,depth=.6,side=.5,slope=1.,outlet=200.,outlet_clear=True,
            diameter=1000.,spacing=250.,culvert_clear=True,lining='Béton',flow=0.,water_depth=.4,roughness=.015,
            source='Dimensions fictives ; aucune étude hydraulique réelle.' if demo else '', hydraulic_ref=''),
        structure=dict(lanes=7.,shoulder_left=1.5,shoulder_right=1.5,declared=10. if demo else 12.,
            layers=[dict(name=n, material=mat, thickness=t, reference=t) for n,mat,t in zip(LAYER_NAMES,MATERIALS,[6.,20.,25.,30.])],
            source='Épaisseurs fictives, reprises de la coupe fournie.' if demo else '', design_ref='', quality_ref=''),
        observation=dict(pothole=0.,shoulder_blocked=False,notes=''),
        criteria=dict(cbr=15.,traffic_low=200000.,traffic_high=500000.,cross=2.5,tolerance=0.,cant_min=3.,cant_max=7.,
            shoulder_min=3.,shoulder_max=4.,shoulder_total=demo,bottom=.5,depth_min=.5,depth_max=.7,
            slope_min=.5,slope_max=2.,outlet=200.,diameter=1000.,spacing=250.,reference='Consignes pédagogiques du projet ; seuils à justifier.'),
        maintenance=dict(pothole=2.,brush_width=1.5,month1=3,month2=10,review=7,drain_cost=0.,brush_cost=0.,events=[]))


def new_project(name='Nouvelle route', demo=False):
    return dict(id=uid(),name=name,location='Site fictif' if demo else '',years=20,start='2026-01-01',
                segments=[new_segment(demo=demo)],demo=demo)


def validate_segment(raw):
    if not isinstance(raw, dict):
        raise ValueError('Tronçon invalide.')
    s=copy.deepcopy(raw)
    s['id']=text(s.get('id'),'ID',60,True);s['name']=text(s.get('name'),'Nom du tronçon',100,True)
    s['start']=numeric(s.get('start'),'PK début',0,1e7);s['end']=numeric(s.get('end'),'PK fin',0,1e7)
    if s['end']<=s['start']:raise ValueError('Le PK de fin doit être supérieur au PK de début.')
    if s.get('alignment') not in ['Alignement droit','Courbe']:raise ValueError('Tracé inconnu.')
    try:
        t,w,b,c,o,a=(s[k] for k in ['traffic','water','structure','criteria','observation','maintenance'])
        if not all(isinstance(x,dict) for x in [t,w,b,c,o,a]):raise ValueError('Rubrique manquante.')
        if t.get('mode') not in MODES or t.get('model') not in ['Sans croissance','Linéaire','Géométrique']:raise ValueError('Modèle de trafic invalide.')
        for k,lo,hi,intg in [('daily',0,100000,False),('interval',.1,1440,False),('hours',.1,24,False),('days',1,365,True),('growth',0,15,False),('share',.1,100,False),('mass',1,150,False),('cam',0,20,False)]:
            t[k]=numeric(t.get(k),k,lo,hi,intg)
        t['source']=text(t.get('source'),'Source trafic',1000)
        if not isinstance(s['soil'],list) or len(s['soil'])>200:raise ValueError('200 essais CBR maximum.')
        for r in s['soil']:
            if not isinstance(r,dict):raise ValueError('Essai CBR invalide.')
            r['pk']=numeric(r.get('pk'),'PK de l’essai',s['start'],s['end'])
            r['cbr']=numeric(r.get('cbr'),'CBR',0,300)
            r['condition']=text(r.get('condition'),'Conditions CBR',150,True)
            r['source']=text(r.get('source'),'Source CBR',200,True)
            r['date']=date.fromisoformat(r['date']).isoformat()
        for k,lo,hi in [('cross_left',0,15),('cross_right',0,15),('cant',0,15),('shoulder_left',0,15),('shoulder_right',0,15),('bottom',.1,5),('depth',.1,3),('side',.1,3),('slope',.01,15),('outlet',1,5000),('diameter',100,5000),('spacing',1,5000),('flow',0,100),('water_depth',.01,3),('roughness',.01,.15)]:
            w[k]=numeric(w.get(k),k,lo,hi)
        if w['water_depth']>w['depth']:raise ValueError('La hauteur d’eau ne peut dépasser la profondeur du fossé.')
        if w.get('shape') not in ['Trapézoïdal','Rectangulaire','Triangulaire','Absent']:raise ValueError('Forme de fossé inconnue.')
        for k in ['outlet_clear','culvert_clear']:w[k]=flag(w.get(k),k)
        for k in ['source','hydraulic_ref']:w[k]=text(w.get(k),k,1000)
        w['lining']=text(w.get('lining'),'Revêtement du fossé',80,True)
        for k in ['lanes','shoulder_left','shoulder_right','declared']:b[k]=numeric(b.get(k),k,0 if k=='declared' else .1,50)
        if not isinstance(b.get('layers'),list) or len(b['layers'])!=4:raise ValueError('Quatre couches requises.')
        for i,l in enumerate(b['layers']):
            l['name']=LAYER_NAMES[i];l['material']=text(l.get('material'),'Matériau',100,True)
            l['thickness']=numeric(l.get('thickness'),'Épaisseur mesurée',.1,150)
            l['reference']=numeric(l.get('reference'),'Épaisseur de référence',.1,150)
        for k in ['source','design_ref','quality_ref']:b[k]=text(b.get(k),k,1000)
        for k,lo,hi in [('cbr',0,100),('traffic_low',0,1e8),('traffic_high',1,1e8),('cross',0,10),('tolerance',0,2),('cant_min',0,15),('cant_max',0,15),('shoulder_min',0,15),('shoulder_max',0,15),('bottom',.1,5),('depth_min',.1,3),('depth_max',.1,3),('slope_min',.01,15),('slope_max',.01,15),('outlet',1,5000),('diameter',100,5000),('spacing',1,5000)]:c[k]=numeric(c.get(k),k,lo,hi)
        for x,y in [('traffic_low','traffic_high'),('cant_min','cant_max'),('shoulder_min','shoulder_max'),('depth_min','depth_max'),('slope_min','slope_max')]:
            if c[x]>c[y]:raise ValueError('Les bornes des critères doivent être croissantes : '+x)
        c['reference']=text(c.get('reference'),'Origine des critères',1000,True);c['shoulder_total']=flag(c.get('shoulder_total'),'Interprétation des accotements')
        o['pothole']=numeric(o.get('pothole'),'Nid-de-poule',0,100);o['shoulder_blocked']=flag(o.get('shoulder_blocked'),'Obstacle');o['notes']=text(o.get('notes'),'Observations',1000)
        for k,lo,hi,intg in [('pothole',.1,20,False),('brush_width',.1,10,False),('month1',1,12,True),('month2',1,12,True),('review',1,20,True),('drain_cost',0,1e9,False),('brush_cost',0,1e9,False)]:a[k]=numeric(a.get(k),k,lo,hi,intg)
        if a['month1']==a['month2']:raise ValueError('Choisir deux mois de curage distincts.')
        if not isinstance(a['events'],list) or len(a['events'])>300:raise ValueError('300 interventions maximum.')
        ids=[]
        for ev in a['events']:
            ev['id']=text(ev.get('id'),'ID intervention',80,True);ids.append(ev['id'])
            ev['date']=date.fromisoformat(ev['date']).isoformat();ev['cost']=numeric(ev.get('cost'),'Coût constaté',0,1e10)
            ev['note']=text(ev.get('note'),'Note intervention',600);ev['done']=flag(ev.get('done'),'Intervention réalisée')
            if not ev['done'] and ev['cost']>0:raise ValueError('Coût constaté sans intervention réalisée.')
            if date.fromisoformat(ev['date'])>date.today():raise ValueError('Date de réalisation future.')
        if len(ids)!=len(set(ids)):raise ValueError('Interventions dupliquées.')
    except (KeyError,TypeError,AttributeError) as exc:raise ValueError('Données incomplètes ou mal structurées.') from exc
    return s


def validate_project(raw):
    if not isinstance(raw,dict):raise ValueError('Projet invalide.')
    p=copy.deepcopy(raw)
    p['id']=text(p.get('id'),'Identifiant',60,True);p['name']=text(p.get('name'),'Nom de route',100,True)
    p['location']=text(p.get('location'),'Localisation',160);p['years']=numeric(p.get('years'),'Horizon',20,40,True)
    p['start']=date.fromisoformat(p['start']).isoformat();p['demo']=flag(p.get('demo'),'Origine fictive')
    if not isinstance(p.get('segments'),list) or not 1<=len(p['segments'])<=20:raise ValueError('Prévoir 1 à 20 tronçons par route.')
    p['segments']=[validate_segment(s) for s in p['segments']]
    if len({s['id'] for s in p['segments']})!=len(p['segments']):raise ValueError('Identifiants de tronçon dupliqués.')
    ordered=sorted(p['segments'],key=lambda s:s['start'])
    if any(a['end']>b['start'] for a,b in zip(ordered,ordered[1:])):raise ValueError('Les tronçons d’une route ne doivent pas se chevaucher.')
    return p


def daily_traffic(t):
    raw=t['daily'] if t['mode']==MODES[0] else 60/t['interval']*t['hours']
    return raw*t['share']/100


def cumulative(flow, growth, years, days=365, model='Géométrique'):
    flow,growth=np.asarray(flow,dtype=float),np.asarray(growth,dtype=float)/100
    if model=='Sans croissance':factor=np.ones_like(growth)*years
    elif model=='Linéaire':factor=years+growth*years*(years-1)/2
    else:
        factor=np.full_like(growth,float(years))
        np.divide(np.expm1(years*np.log1p(growth)),growth,out=factor,where=np.abs(growth)>1e-12)
    return days*flow*factor


def traffic_table(p,s):
    t=s['traffic'];flow=daily_traffic(t)
    return pd.DataFrame({'Année':range(1,p['years']+1),'PL cumulés sur la voie étudiée':[float(cumulative(flow,t['growth'],y,t['days'],t['model'])) for y in range(1,p['years']+1)]})


def ditch_capacity(w):
    if w['shape']!='Trapézoïdal':return None
    y,b,z=w['water_depth'],w['bottom'],w['side']
    area=(b+z*y)*y;perimeter=b+2*y*math.sqrt(1+z*z)
    return dict(area=area,radius=area/perimeter,flow=area/w['roughness']*(area/perimeter)**(2/3)*math.sqrt(w['slope']/100))


def diagnose(p,s):
    c=s['criteria'];w=s['water'];b=s['structure'];t=s['traffic'];rows=[]
    def check(group,label,ready,good,observed,expected,action):
        rows.append(dict(module=group,controle=label,statut='unknown' if not ready else 'ok' if good else 'bad',valeur=observed,critere=expected,action=action))
    total=float(cumulative(daily_traffic(t),t['growth'],p['years'],t['days'],t['model']))
    check('Trafic','Cumul de poids lourds',bool(t['source']),total<=c['traffic_high'],f'{total:,.0f} PL'.replace(',',' '),f"≤ {c['traffic_high']:,.0f} PL (plafond du projet)".replace(',',' '),'Revoir le trafic de référence et faire examiner la structure pour cette exposition.')
    check('Plate-forme','CBR minimal observé',bool(s['soil']),bool(s['soil']) and min(r['cbr'] for r in s['soil'])>c['cbr'],fmt(min((r['cbr'] for r in s['soil']),default=0)) if s['soil'] else 'Aucun essai',f"> {fmt(c['cbr'])}",'Examiner les points faibles, les conditions d’essai et les traitements possibles avec l’étude géotechnique.')
    check('Plate-forme','Conditions des essais CBR',bool(s['soil']),all(r['condition']!='Non précisé' for r in s['soil']),', '.join(sorted({r['condition'] for r in s['soil']})) or 'Non renseignées','Conditions identifiées ; comparabilité à examiner','Préciser immersion, état hydrique, compactage et représentativité des essais.')
    ready=bool(w['source'])
    if s['alignment']=='Alignement droit':
        good=all(abs(w[k]-c['cross'])<=c['tolerance']+1e-9 for k in ['cross_left','cross_right'])
        check('Drainage','Pentes transversales',ready,good,f"{fmt(w['cross_left'])} / {fmt(w['cross_right'])} %",f"{fmt(c['cross'])} % ± {fmt(c['tolerance'])} point",'Mesurer le profil transversal et corriger les zones de stagnation.')
    else:check('Drainage','Dévers en courbe',ready,c['cant_min']<=w['cant']<=c['cant_max'],f"{fmt(w['cant'])} %",f"{fmt(c['cant_min'])} à {fmt(c['cant_max'])} %",'Faire vérifier le dévers avec le tracé et la vitesse de projet.')
    check('Drainage','Pentes des accotements',ready and c['shoulder_total'],all(c['shoulder_min']<=w[k]<=c['shoulder_max'] for k in ['shoulder_left','shoulder_right']),f"{fmt(w['shoulder_left'])} / {fmt(w['shoulder_right'])} %",f"{fmt(c['shoulder_min'])} à {fmt(c['shoulder_max'])} % en pente totale",'Confirmer l’interprétation des pentes et vérifier leur sens vers l’extérieur.')
    for label,good,observed,expected,action in [
        ('Forme du fossé',w['shape']=='Trapézoïdal',w['shape'],'Trapézoïdal','Adapter le fossé au profil retenu.'),
        ('Largeur au fond',w['bottom']>=c['bottom'],f"{fmt(w['bottom'])} m",f"≥ {fmt(c['bottom'])} m",'Vérifier la largeur au fond, distincte de la largeur en tête.'),
        ('Profondeur du fossé',c['depth_min']<=w['depth']<=c['depth_max'],f"{fmt(w['depth'])} m",f"{fmt(c['depth_min'])} à {fmt(c['depth_max'])} m",'Réexaminer le profil et le raccordement aux exutoires.'),
        ('Pente longitudinale du fossé',c['slope_min']<=w['slope']<=c['slope_max'],f"{fmt(w['slope'])} %",f"{fmt(c['slope_min'])} à {fmt(c['slope_max'])} %",'Examiner stagnation, érosion et protection du fossé.'),
        ('Espacement des exutoires',w['outlet']<=c['outlet'],f"{fmt(w['outlet'])} m",f"≤ {fmt(c['outlet'])} m",'Vérifier les exutoires réels, leur accessibilité et leur rejet.'),
        ('État des exutoires',w['outlet_clear'],'Libre' if w['outlet_clear'] else 'Obstrué','Écoulement libre déclaré','Dégager les exutoires et inspecter les dommages.'),
        ('Diamètre intérieur des buses',w['diameter']>=c['diameter'],f"{fmt(w['diameter'],0)} mm",f"≥ {fmt(c['diameter'],0)} mm",'Faire vérifier le diamètre au regard du débit et des conditions hydrauliques.'),
        ('Espacement des buses',w['spacing']<=c['spacing'],f"{fmt(w['spacing'])} m",f"≤ {fmt(c['spacing'])} m",'Examiner les points bas et bassins versants ; une règle d’espacement ne suffit pas.'),
        ('État des buses',w['culvert_clear'],'Libre' if w['culvert_clear'] else 'Obstrué','Écoulement libre déclaré','Curer les buses et contrôler leurs entrées et sorties.')]:
        check('Drainage',label,ready,good,observed,expected,action)
    check('Drainage','Étude hydraulique référencée',bool(w['hydraulic_ref']),True,w['hydraulic_ref'] or 'Absente','Référence documentaire (non contrôlée par le logiciel)','Documenter débits, exutoires, niveaux amont/aval et capacité des buses.')
    actual=b['lanes']+b['shoulder_left']+b['shoulder_right']
    check('Structure','Largeur de plate-forme',bool(b['source']) and b['declared']>0,abs(actual-b['declared'])<.01,f"Somme {fmt(actual)} m ; déclarée {fmt(b['declared'])} m",'Chaussée + deux accotements, hors fossés','Réconcilier les cotes et préciser les autres bandes éventuelles.')
    for l in b['layers']:
        check('Structure','Épaisseur : '+l['name'],bool(b['source']),l['thickness']+1e-9>=l['reference'],f"{fmt(l['thickness'])} cm",f"≥ référence {fmt(l['reference'])} cm",'Faire examiner l’écart au dossier ; plus épais ne prouve pas une meilleure conception.')
    for key,label in [('design_ref','Dimensionnement référencé'),('quality_ref','Contrôles des matériaux référencés')]:
        check('Structure',label,bool(b[key]),True,b[key] or 'Absent','Référence documentaire (non contrôlée par le logiciel)','Documenter justification mécanique, matériaux, compactage et qualité d’exécution.')
    return rows


def simulate_traffic(s,years,count,seed,flow_bounds,growth_bounds):
    count=numeric(count,'Tirages',1000,100000,True);seed=numeric(seed,'Graine',0,2**32-1,True)
    for bounds,hi in [(flow_bounds,100000),(growth_bounds,15)]:
        if len(bounds)!=3:raise ValueError('Trois paramètres par loi triangulaire.')
        vals=[numeric(x,'Paramètre triangulaire',0,hi) for x in bounds]
        if vals!=sorted(vals):raise ValueError('Respecter minimum ≤ mode ≤ maximum.')
    rng=np.random.default_rng(seed)
    def draw(a):return np.full(count,float(a[0])) if a[0]==a[2] else rng.triangular(*a,size=count)
    flow=draw(flow_bounds)*s['traffic']['share']/100;growth=draw(growth_bounds)
    matrix=np.column_stack([cumulative(flow,growth,y,s['traffic']['days'],s['traffic']['model']) for y in range(1,years+1)])
    limit=s['criteria']['traffic_high'];hits=matrix>limit
    first=np.where(hits.any(axis=1),hits.argmax(axis=1)+1,0)
    frame=pd.DataFrame({'PL_jour_voie':flow,'croissance_pct':growth,'PL_cumules':matrix[:,-1],'depassement':hits[:,-1],'premiere_annee_depassement':first})
    bands=pd.DataFrame({'Année':range(1,years+1),'P05':np.quantile(matrix,.05,axis=0),'Médiane':np.median(matrix,axis=0),'P95':np.quantile(matrix,.95,axis=0)})
    return frame,bands


def wilson(k,n):
    z=1.96;p=k/n;den=1+z*z/n
    center=(p+z*z/(2*n))/den;half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return max(0.,center-half),min(1.,center+half)


def maintenance_plan(p,s):
    a=s['maintenance'];start=date.fromisoformat(p['start']);end=date(start.year+p['years'],start.month,start.day if not(start.month==2 and start.day==29) else 28)
    rows=[]
    for year in range(start.year,end.year+1):
        for month,kind,cost in [(a['month1'],'Curage fossés et buses',a['drain_cost']),(a['month2'],'Curage fossés et buses',a['drain_cost']),(a['month1'],'Débroussaillage',a['brush_cost'])]:
            when=date(year,month,15)
            if start<=when<end:
                rows.append(dict(id=f'{year}-{month:02d}-{kind}',date=when.isoformat(),action=kind,cout_prevu=cost,
                    detail=f"Bande {fmt(a['brush_width'])} m de chaque côté" if kind=='Débroussaillage' else 'Deux campagnes annuelles ; mois à adapter aux pluies.'))
    for elapsed in range(a['review'],p['years'],a['review']):
        when=date(start.year+elapsed,start.month,min(start.day,28))
        rows.append(dict(id=f'revue-{elapsed}',date=when.isoformat(),action='Inspection du revêtement',cout_prevu=0.,detail='Décider d’un enduit selon l’état ; aucun renouvellement automatique.'))
    events={x['id']:x for x in a['events']}
    for r in rows:
        ev=events.get(r['id'],{});r.update(realise=ev.get('done',False),date_reelle=ev.get('date',''),cout_constate=ev.get('cost',0.),note=ev.get('note',''))
    return pd.DataFrame(rows).sort_values(['date','action']).reset_index(drop=True)


def db_file():
    default=Path.home()/'Documents/Codex/data/route20lab'
    folder=Path(os.environ.get('ROUTE20LAB_DATA_DIR',str(default))).expanduser().resolve()
    appdir=Path(__file__).resolve().parent
    if folder==appdir or appdir in folder.parents:raise ValueError('Le stockage doit rester hors du dossier livré.')
    folder.mkdir(parents=True,exist_ok=True)
    return folder/'route20lab.sqlite3'


def load_store():
    with closing(sqlite3.connect(db_file(),timeout=15)) as conn:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO workspace VALUES (1,0,?)',('[]',))
        rev,payload=conn.execute('SELECT revision,payload FROM workspace WHERE id=1').fetchone()
    rows=json.loads(payload)
    if not isinstance(rows,list) or len(rows)>20:raise ValueError('Registre invalide.')
    return [validate_project(x) for x in rows],rev


def save_store(rows,revision):
    if len(rows)>20:raise ValueError('20 routes maximum.')
    valid=[validate_project(x) for x in rows]
    if len({x['id'] for x in valid})!=len(valid):raise ValueError('Identifiants de route dupliqués.')
    payload=json.dumps(valid,ensure_ascii=False,allow_nan=False)
    if len(payload.encode())>8_000_000:raise ValueError('Registre limité à 8 Mo.')
    with closing(sqlite3.connect(db_file(),timeout=15)) as conn:
        with conn:
            result=conn.execute('UPDATE workspace SET revision=revision+1,payload=? WHERE id=1 AND revision=?',(payload,revision))
            if result.rowcount!=1:raise ValueError('Registre modifié dans un autre onglet : recharger le registre et recommencer.')
    return revision+1


def backup_bytes(rows):
    return json.dumps(dict(app='Route20Lab',version=VERSION,projects=rows),ensure_ascii=False,indent=2,allow_nan=False).encode()


def restore_bytes(raw,allow_demo=False):
    if len(raw)>8_000_000:raise ValueError('Sauvegarde limitée à 8 Mo.')
    try:
        data=json.loads(raw)
        if not isinstance(data,dict) or data.get('app')!='Route20Lab' or not isinstance(data.get('projects'),list):raise ValueError('Sauvegarde Route20Lab attendue.')
        if not 1<=len(data['projects'])<=20:raise ValueError('Sauvegarde vide ou excessive.')
        rows=[validate_project(x) for x in data['projects']]
    except (json.JSONDecodeError,UnicodeDecodeError,KeyError,TypeError) as exc:raise ValueError('Fichier JSON invalide.') from exc
    if not allow_demo and any(x['demo'] for x in rows):raise ValueError('Une démonstration fictive doit être restaurée dans l’espace fictif.')
    for p in rows:
        p['id']=uid();p['name']=p['name'][:88]+' (copie)'
        for s in p['segments']:s['id']=uid()
    return rows


def csv_bytes(df):
    df=df.copy()
    for col in df.select_dtypes(include=['object','str']).columns:
        df[col]=df[col].map(lambda x:"'"+x if isinstance(x,str) and x.lstrip().startswith(('=','+','-','@')) else x)
    return df.to_csv(index=False,sep=';',decimal=',').encode('utf-8-sig')


def import_cbr(raw,extension,s):
    if len(raw)>2_000_000:raise ValueError('Fichier limité à 2 Mo.')
    try:
        df=pd.read_excel(io.BytesIO(raw),engine='openpyxl') if extension=='xlsx' else pd.read_csv(io.BytesIO(raw),sep=None,engine='python',encoding='utf-8-sig',dtype=str)
        if set(df.columns)!={'pk','cbr','condition','date','source'}:raise ValueError('Colonnes : pk, cbr, condition, date, source.')
        if not 1<=len(df)<=200:raise ValueError('1 à 200 essais attendus.')
        if df.isna().any().any():raise ValueError('Chaque champ d’essai est obligatoire.')
        rows=df.to_dict('records')
        for r in rows:
            for k in ['pk','cbr']:r[k]=float(str(r[k]).replace(',','.'))
            r['date']=str(r['date'])[:10]
        trial=copy.deepcopy(s);trial['soil']=rows
        return validate_segment(trial)['soil']
    except (ValueError,TypeError,KeyError,OSError,UnicodeError) as exc:raise ValueError('Import CBR refusé : '+str(exc)) from None


def excel_bytes(p,s):
    out=io.BytesIO();df=pd.DataFrame(diagnose(p,s));df['statut']=df.statut.map(STATUS)
    tables={'Diagnostic':df,'Trafic':traffic_table(p,s),'CBR':pd.DataFrame(s['soil']),'Couches':pd.DataFrame(s['structure']['layers']),'Entretien':maintenance_plan(p,s),'Realisations':pd.DataFrame(s['maintenance']['events'])}
    with pd.ExcelWriter(out,engine='openpyxl') as writer:
        for name,frame in tables.items():
            frame.to_excel(writer,sheet_name=name,index=False)
            ws=writer.sheets[name];ws.freeze_panes='A2'
            for row in ws:
                for cell in row:
                    if isinstance(cell.value,str):cell.data_type='s'
            for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=26
    return out.getvalue()


def pdf_bytes(p,s):
    from reportlab import __file__ as reportlab_file
    fontdir=Path(reportlab_file).parent/'fonts'
    for name,file in [('R20','Vera.ttf'),('R20B','VeraBd.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(name,str(fontdir/file)))
    pdfmetrics.registerFontFamily('R20',normal='R20',bold='R20B',italic='R20',boldItalic='R20B')
    body=ParagraphStyle('Body',fontName='R20',fontSize=9,leading=13,spaceAfter=7,textColor=colors.HexColor(INK))
    heading=ParagraphStyle('Heading',parent=body,fontName='R20B',fontSize=14,leading=19,spaceBefore=12,keepWithNext=True)
    small=ParagraphStyle('Small',parent=body,fontSize=8,leading=11)
    out=io.BytesIO();story=[]
    def par(v,style=body):return Paragraph(html.escape(str(v)).replace('\n','<br/>'),style)
    def grid(headers,rows,widths):
        data=[[par(x,small) for x in headers]]+[[par(x,small) for x in r] for r in rows]
        t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor(YELLOW)),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F1F4EE')]),('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]));story.append(t)
    story.extend([par('Route20Lab · Note de diagnostic',heading),par(('EXEMPLE FICTIF · ' if p['demo'] else '')+p['name']+' / '+s['name']),par(f"PK {fmt(s['start'])} à {fmt(s['end'])} m · Horizon {p['years']} ans"),par('Les critères du projet servent à une comparaison pédagogique. Une durée de service de 20 ans n’est ni calculée ni garantie. Les références documentaires sont déclarées, non expertisées.')])
    traffic=float(cumulative(daily_traffic(s['traffic']),s['traffic']['growth'],p['years'],s['traffic']['days'],s['traffic']['model']))
    story.append(par('Exposition au trafic',heading));story.append(par(f"{fmt(daily_traffic(s['traffic']))} PL/jour sur la voie étudiée ; cumul {fmt(traffic,0)} PL. Modèle : {s['traffic']['model']}. Source : {s['traffic']['source'] or 'à renseigner'}."))
    for group in ['Trafic','Plate-forme','Drainage','Structure']:
        story.append(par(group,heading))
        rows=[r for r in diagnose(p,s) if r['module']==group]
        grid(['Contrôle / résultat','Valeur déclarée','Critère'],[[r['controle']+'\n'+STATUS[r['statut']],r['valeur'],r['critere']] for r in rows],[170,167,170])
    story.append(par('Corrections et points à documenter',heading))
    for r in diagnose(p,s):
        if r['statut']!='ok':story.append(par(r['controle']+' : '+r['action']))
    story.append(par('Structure renseignée',heading))
    grid(['Couche','Matériau','Épaisseur / référence'],[[l['name'],l['material'],f"{fmt(l['thickness'])} / {fmt(l['reference'])} cm"] for l in s['structure']['layers']],[100,260,147])
    story.append(par('Observations et entretien prioritaire',heading))
    obs=s['observation'];maintenance=s['maintenance']
    story.append(par(f"Nid-de-poule maximal déclaré : {fmt(obs['pothole'])} cm ; seuil du projet : {fmt(maintenance['pothole'])} cm. "+('Inspection et réparation adaptée à organiser.' if obs['pothole']>=maintenance['pothole'] else 'Surveillance à poursuivre.')))
    story.append(par('Accotements : '+('obstacle à l’eau déclaré ; dérasement à examiner.' if obs['shoulder_blocked'] else 'aucun bourrelet bloquant déclaré.')))
    if obs['notes']:story.append(par('Note de terrain : '+obs['notes']))
    story.append(par('Entretien : premiers rendez-vous',heading))
    plan=maintenance_plan(p,s).head(12)
    grid(['Date prévue','Intervention','État'],[[r['date'],r['action'],'Réalisée' if r['realise'] else 'À organiser'] for _,r in plan.iterrows()],[95,285,127])
    story.append(par('Les dates sont proposées selon les mois saisis, à adapter aux saisons locales. Le calendrier complet et les coûts sont dans Excel. Réparer les nids-de-poule selon inspection, curer fossés et buses, débroussailler et déraser les accotements qui font obstacle à l’eau. À l’échéance de revue, examiner l’opportunité d’un enduit selon l’état.'))
    story.append(par('Origine des hypothèses',heading));story.append(par(s['criteria']['reference']))
    story.append(par('Coupe fournie par l’utilisateur : roulement 6 cm, base 20 cm, fondation 25 cm, forme 30 cm. L’image mentionne 12 m de plate-forme mais 7 + 1,5 + 1,5 = 10 m hors fossés ; fond de fossé 0,40 m sur l’image contre 0,50 m dans les consignes. Ces valeurs ne constituent pas un catalogue universel.'))
    story.append(par('Méthodes et limites',heading));story.append(par('Le CBR minimal observé est comparé strictement au seuil, sans extrapolation aux zones non testées. Le cumul de PL n’est pas une masse en tonnes ni un cumul d’essieux équivalents. Les simulations éventuelles s’exportent séparément. L’épaisseur seule ne vérifie ni fatigue, ni déformation, ni qualité des matériaux.'))
    for label,url in SOURCES.items():story.append(par(label+' : '+url,small))
    def footer(c,doc):
        c.setFillColor(colors.HexColor(INK));c.rect(0,831,596,11,fill=1,stroke=0)
        c.setFont('R20',8);c.drawString(44,25,'Route20Lab · Comparaison pédagogique · '+('FICTIF' if p['demo'] else 'Données déclarées'));c.drawRightString(551,25,str(doc.page))
    SimpleDocTemplate(out,pagesize=(595.28,841.89),leftMargin=44,rightMargin=44,topMargin=38,bottomMargin=46,title='Route20Lab - Diagnostic et entretien').build(story,onFirstPage=footer,onLaterPages=footer)
    return out.getvalue()


def fmt(x,digits=2):
    return f'{x:,.{digits}f}'.replace(',',' ').replace('.',',')


def esc(x):return html.escape(str(x))


CSS='''<style>
:root{--ink:#172326;--teal:#087F83;--yellow:#E9ED65;--paper:#F3F4EC}
[data-testid="stApp"]{background:var(--paper);color:var(--ink)}
[data-testid="stMainBlockContainer"]{max-width:1510px;padding-top:4.8rem;padding-bottom:3rem}
[data-testid="stSidebar"]{background:#172326;color:#F2F5EA;border-right:1px solid #3C4B4C}
[data-testid="stSidebar"] p,[data-testid="stSidebar"] label,[data-testid="stSidebar"] a{color:#F2F5EA}
[data-testid="stSidebar"] [data-testid="stButton"] button{background:#283A3D;color:#FFF;border:1px solid #5E7675}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover{background:#3A5353;border-color:#E9ED65}
[data-testid="stSidebar"] [aria-current="page"]{background:#344B49;border-left:3px solid #E9ED65}
.side-logo{font-size:1.8rem;font-weight:850;letter-spacing:-1px;margin:.3rem 0 0}.side-logo b{color:#E9ED65}.side-desc{font-size:.78rem;color:#C4D1C9;margin:.2rem 0 1.1rem}
.mast{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:20px;border-bottom:1px solid #D3D9CE;padding-bottom:13px}.wordmark{font-size:1.2rem;font-weight:850}.wordmark b{color:#087F83}.pill{border-radius:30px;padding:6px 10px;background:#E9ED65;font-size:.72rem;font-weight:750;color:#172326;white-space:normal}
.hero{background:#172326;border-radius:22px;overflow:hidden;display:grid;grid-template-columns:1.1fr 1fr;align-items:center;min-height:255px;margin-bottom:22px;box-shadow:0 10px 28px #17232616}.hero-copy{padding:28px 30px;color:#F6F7EF}.hero .eyebrow{font-size:.7rem;letter-spacing:2px;color:#E9ED65}.hero h2{font-size:clamp(1.6rem,3vw,2.6rem);line-height:1.14;color:#FFF!important;margin:12px 0}.hero p{color:#D4E0D8;line-height:1.6;font-size:.92rem;margin:10px 0}.hero img{width:100%;max-height:280px;object-fit:contain}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin:8px 0 22px}.kpi{background:#FFF;border:1px solid #D8DFD2;border-radius:15px;padding:17px 18px;box-shadow:0 4px 10px #17232605;border-top:4px solid #087F83}.kpi:nth-child(2){border-top-color:#BDC52C}.kpi:nth-child(3){border-top-color:#B47E50}.kpi:nth-child(4){border-top-color:#546D7A}.kpi label{display:block;color:#536563;font-size:.78rem;line-height:1.4;overflow-wrap:anywhere}.kpi strong{display:block;font-size:clamp(1.3rem,2vw,1.9rem);letter-spacing:-.5px;margin:8px 0;line-height:1.2;overflow-wrap:anywhere}.kpi span{font-size:.73rem;color:#647571;line-height:1.4;display:block}
.note{padding:14px 18px;border-left:4px solid #087F83;background:#E2EEEA;border-radius:0 10px 10px 0;margin:8px 0 18px;font-size:.9rem;line-height:1.6}.mini-status{border-radius:10px;padding:10px 12px;background:#E2EEEA;font-size:.83rem;line-height:1.5;margin:4px 0 12px}.road-card{border-left:5px solid #087F83;padding-left:13px;margin:5px 0}.road-card small{color:#637773}.road-card h3{font-size:1.1rem;margin:6px 0;color:#172326}.foot{margin-top:36px;padding-top:16px;border-top:1px solid #D3D9CE;font-size:.77rem;color:#596C67;line-height:1.6}
[data-testid="stForm"]{background:#FFF;border-radius:16px;border-color:#D8DFD2}
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:16px}
[data-testid="stBaseButton-primary"],[data-testid="stBaseButton-primaryFormSubmit"]{background:#E9ED65!important;border-color:#CCD444!important;color:#172326!important;font-weight:750}
[data-testid="stMain"] button p{white-space:normal;overflow-wrap:anywhere}
h1{letter-spacing:-1px!important}h2,h3{color:#172326!important}
@media(max-width:950px){.hero{grid-template-columns:1fr 0.8fr}.hero-copy{padding:24px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:640px){[data-testid="stMainBlockContainer"]{padding:4.4rem .85rem 2rem}.hero{grid-template-columns:1fr}.hero-copy{padding:23px 20px}.hero img{max-height:160px}.kpis{gap:9px}.kpi{padding:13px 12px}.kpi strong{font-size:1.35rem}.mast{align-items:flex-start}.pill{max-width:120px}.note{padding:11px 13px}.hero h2{font-size:1.9rem}}
</style>'''


def hero_art():
    svg='''<svg xmlns="http://www.w3.org/2000/svg" width="650" height="340" viewBox="0 0 650 340"><defs><pattern id="g" width="35" height="35" patternUnits="userSpaceOnUse"><path d="M35 0H0V35" fill="none" stroke="#304443" stroke-width=".8"/></pattern></defs><rect width="650" height="340" fill="url(#g)"/><path d="M-40 280Q160 240 270 125T700 90" fill="none" stroke="#0E6668" stroke-width="133"/><path d="M-40 280Q160 240 270 125T700 90" fill="none" stroke="#374C4D" stroke-width="105"/><path d="M-40 280Q160 240 270 125T700 90" fill="none" stroke="#C0D2C7" stroke-width="83"/><path d="M-40 280Q160 240 270 125T700 90" fill="none" stroke="#172326" stroke-width="78"/><path d="M-40 280Q160 240 270 125T700 90" fill="none" stroke="#E9ED65" stroke-width="4" stroke-dasharray="19 15"/><circle cx="282" cy="113" r="20" fill="#E9ED65"/><circle cx="282" cy="113" r="7" fill="#172326"/><path d="M291 95L345 30H475" stroke="#E9ED65" fill="none" stroke-width="2"/><text x="355" y="22" fill="#E9ED65" font-family="sans-serif" font-size="13">TRONÇON SOUS ÉTUDE</text><rect x="389" y="218" width="179" height="76" rx="12" fill="#233B3B"/><text x="407" y="244" fill="#C5D6CE" font-family="sans-serif" font-size="12">HORIZON DE L’ÉTUDE</text><text x="407" y="277" fill="#E9ED65" font-family="sans-serif" font-weight="bold" font-size="30">20 ANS +</text></svg>'''
    return 'data:image/svg+xml;base64,'+base64.b64encode(svg.encode()).decode()


def title(name,subtitle):
    st.title(name);st.caption(subtitle)


def note(message):st.html('<div class="note">'+message+'</div>')


def metrics(items):
    st.html('<div class="kpis">'+''.join(f'<div class="kpi"><label>{esc(label)}</label><strong>{esc(value)}</strong><span>{esc(detail)}</span></div>' for label,value,detail in items)+'</div>')


def chart(fig,alt,key=None,height=350):
    fig.update_layout(template='plotly_white',paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor='#FFFFFF',height=height,margin=dict(l=48,r=22,t=30,b=65),font=dict(family='Arial',size=12,color=INK),legend=dict(orientation='h',y=-.25),colorway=[TEAL,'#B3BE32','#B47E50','#627D8A'])
    st.plotly_chart(fig,width='stretch',config={'displaylogo':False,'responsive':True},alt=alt,key=key)


def cross_section(s):
    b=s['structure'];w=s['water'];half=b['lanes']/2;sl=b['shoulder_left'];sr=b['shoulder_right']
    x=np.array([-half-sl,-half,0,half,half+sr]);slopes=w['cross_left']/100,w['cross_right']/100
    y=np.array([-half*slopes[0]-sl*w['shoulder_left']/100,-half*slopes[0],0,-half*slopes[1],-half*slopes[1]-sr*w['shoulder_right']/100])
    if s['alignment']=='Courbe':y=-x*w['cant']/100
    fig=go.Figure();top=y.copy()
    for l,color in zip(b['layers'],LAYER_COLORS):
        bottom=top-l['thickness']/100
        fig.add_trace(go.Scatter(x=np.r_[x,x[::-1]],y=np.r_[top,bottom[::-1]],mode='lines',fill='toself',fillcolor=color,line=dict(width=1,color='#FFFFFF'),name=l['name'],hovertemplate=esc(l['name'])+' : '+fmt(l['thickness'])+' cm<extra></extra>'))
        top=bottom
    fig.add_trace(go.Scatter(x=np.r_[x,x[::-1]],y=np.r_[top,(top-.35)[::-1]],mode='lines',fill='toself',fillcolor='#E2D4B6',line=dict(color='#B0A286',width=1),name='Sol support',hoverinfo='skip'))
    for side in [-1,1]:
        edge=x[0] if side==-1 else x[-1];z=w['side'];base=w['bottom'];depth=w['depth'];extent=base+2*z*depth
        xx=np.array([0,z*depth,z*depth+base,extent])*side+edge+side*.15
        yy=np.array([y[0] if side==-1 else y[-1],-depth,-depth,y[0] if side==-1 else y[-1]])
        fig.add_trace(go.Scatter(x=xx,y=yy,mode='lines',line=dict(color=TEAL,width=4),name='Fossé' if side==-1 else 'Fossé droit',showlegend=side==-1,hovertemplate='Fossé illustratif<extra></extra>'))
    fig.add_annotation(x=0,y=.18,text=f"Chaussée {fmt(b['lanes'])} m · plate-forme {fmt(b['lanes']+sl+sr)} m",showarrow=False,font=dict(size=11))
    fig.update_xaxes(title='Largeur transversale (m)',zeroline=False);fig.update_yaxes(title='Niveau relatif (m)',range=[float(top.min())-.5,.35],zeroline=False)
    return fig


def projects():return st.session_state.demo_projects if st.session_state.demo else st.session_state.projects


def active():
    p=next((p for p in projects() if p['id']==st.session_state.project_id),None)
    s=next((s for s in p['segments'] if s['id']==st.session_state.segment_id),p['segments'][0]) if p else None
    return p,s


def commit(rows,pid=None,sid=None):
    rows=[validate_project(p) for p in rows]
    if len(rows)>20:raise ValueError('20 routes maximum.')
    if st.session_state.demo:st.session_state.demo_projects=rows
    else:
        st.session_state.revision=save_store(rows,st.session_state.revision);st.session_state.projects=rows
    if pid:st.session_state.project_id=pid
    if sid:st.session_state.segment_id=sid
    st.session_state.simulation=None;st.session_state.flash='Modifications enregistrées.'


def update(p,s=None):
    q=copy.deepcopy(p)
    if s:q['segments']=[s if x['id']==s['id'] else x for x in q['segments']]
    commit([q if x['id']==q['id'] else x for x in projects()],q['id'],s['id'] if s else q['segments'][0]['id'])
    st.rerun()


def demo():
    if not st.session_state.demo_projects:st.session_state.demo_projects=[new_project('Route des Collines · exemple fictif',True)]
    st.session_state.demo=True;st.session_state.project_id=st.session_state.demo_projects[0]['id'];st.session_state.segment_id=st.session_state.demo_projects[0]['segments'][0]['id'];st.session_state.simulation=None


def personal():
    st.session_state.demo=False;st.session_state.project_id=projects()[0]['id'] if projects() else None;st.session_state.segment_id=None;st.session_state.simulation=None


def require():
    p,s=active()
    if p is None:
        note('Créez une route dans <b>Routes et tronçons</b> ou chargez l’exemple fictif pour explorer les outils.')
    return p,s


def diagnostic_table(p,s,group=None):
    rows=diagnose(p,s)
    if group:rows=[r for r in rows if r['module']==group]
    df=pd.DataFrame(rows);df['statut']=df['statut'].map(STATUS)
    st.dataframe(df.rename(columns={'module':'Rubrique','controle':'Contrôle','statut':'Résultat','valeur':'Valeur','critere':'Critère du projet','action':'Suite proposée'}),hide_index=True,alt='Diagnostic détaillé : critères satisfaits, écarts et informations manquantes')


def dashboard():
    p,s=active()
    st.html(f'<div class="hero"><div class="hero-copy"><div class="eyebrow">OBSERVATOIRE ROUTIER / ROUTE20LAB</div><h2>{esc(p["name"]) if p else "Une route. Vingt ans à préparer."}</h2><p>Trafic, sol, eau et structure : repérez les écarts, comprenez les hypothèses et organisez l’entretien.</p><span class="pill">{esc(s["name"]) if s else "Commencez votre étude"}</span></div><img src="{hero_art()}" alt="Illustration routière, tronçon sous étude et horizon de 20 ans"/></div>')
    if not p:
        st.button('Explorer l’exemple fictif',type='primary',icon=':material/play_arrow:',on_click=demo)
        st.page_link(PAGES[1],label='Créer ma route',icon=':material/add_circle:');return
    rows=diagnose(p,s);counts={k:sum(r['statut']==k for r in rows) for k in STATUS}
    total=traffic_table(p,s).iloc[-1,1]
    metrics([('Horizon étudié',f"{p['years']} ans",'Objectif, pas durée prédite'),('Trafic cumulé',fmt(total/1e6,3)+' M PL','Sur la voie étudiée'),('Écarts détectés',str(counts['bad']),'Par rapport aux critères saisis'),('À documenter',str(counts['unknown']),'Données ou études manquantes')])
    note('Conclusion : '+('des écarts demandent une correction.' if counts['bad'] else 'des informations restent à compléter.' if counts['unknown'] else 'les critères saisis sont satisfaits.')+' <b>La tenue mécanique sur 20 ans reste à justifier par l’étude de chaussée.</b>')
    left,right=st.columns([1.5,1])
    with left:
        st.subheader('La route, couche par couche');chart(cross_section(s),'Coupe transversale illustrative calculée sur les dimensions renseignées',key='home_cut',height=370)
        st.caption('Échelles verticale et horizontale différentes. Fossés illustratifs ; implantation des buses à examiner en profil longitudinal.')
    with right:
        st.subheader('Quatre regards sur le tronçon')
        for group,idx in [('Trafic',2),('Plate-forme',3),('Drainage',4),('Structure',5)]:
            r=[x for x in rows if x['module']==group];bad=sum(x['statut']=='bad' for x in r);unknown=sum(x['statut']=='unknown' for x in r)
            with st.container(border=True):
                st.markdown('**'+group+'**');st.caption(f'{bad} écart(s) · {unknown} à renseigner')
                st.page_link(PAGES[idx],label='Examiner '+group.lower(),icon=ICONS['bad' if bad else 'unknown' if unknown else 'ok'])
    st.subheader('Les tronçons de cette route')
    segment_cards(p)
    with st.expander('Tous les contrôles et les suites proposées'):diagnostic_table(p,s)


def choose(pid,sid):
    st.session_state.project_id=pid;st.session_state.segment_id=sid;st.session_state.simulation=None


def segment_cards(p):
    cols=st.columns(2)
    for i,s in enumerate(p['segments']):
        with cols[i%2],st.container(border=True,key='segment_'+s['id']):
            rows=diagnose(p,s);bad=sum(r['statut']=='bad' for r in rows)
            st.html(f'<div class="road-card"><small>PK {fmt(s["start"],0)} → {fmt(s["end"],0)} m</small><h3>{esc(s["name"])}</h3><small>{bad} écart(s) · {esc(s["alignment"])}</small></div>')
            st.button('Tronçon actif' if active()[1] and s['id']==active()[1]['id'] else 'Ouvrir ce tronçon',disabled=bool(active()[1] and s['id']==active()[1]['id']),key='open_'+s['id'],on_click=choose,args=(p['id'],s['id']),icon=':material/arrow_outward:',width='stretch')


def project_page():
    title('Routes et tronçons','Des dossiers indépendants et des tronçons ouverts depuis leurs cartes.')
    with st.expander('Créer une route',expanded=not bool(projects())):
        with st.form('new_route',clear_on_submit=True):
            name=st.text_input('Nom de la route',max_chars=100);loc=st.text_input('Localisation',max_chars=160)
            if st.form_submit_button('Créer la route',type='primary'):
                q=new_project(text(name,'Nom de route',100,True),st.session_state.demo);q['location']=loc
                commit(projects()+[q],q['id'],q['segments'][0]['id']);st.rerun()
    cols=st.columns(2)
    for i,q in enumerate(projects()):
        with cols[i%2],st.container(border=True):
            st.subheader(q['name']);st.caption(f"{q['location']} · {len(q['segments'])} tronçon(s) · {q['years']} ans")
            st.button('Ouvrir cette route',key='route_'+q['id'],on_click=choose,args=(q['id'],q['segments'][0]['id']),width='stretch')
    p,s=active()
    if not p:return
    st.subheader('Objectifs et identité')
    with st.form('identity_'+p['id']):
        name=st.text_input('Nom du projet',p['name'],max_chars=100);location=st.text_input('Localisation du projet',p['location'],max_chars=160)
        a,b=st.columns(2);start=a.date_input('Mise en service / origine du calendrier',date.fromisoformat(p['start']));years=b.number_input('Horizon de l’étude (ans)',20,40,p['years'])
        if st.form_submit_button('Enregistrer la route',type='primary'):
            q=copy.deepcopy(p);q.update(name=name,location=location,start=start.isoformat(),years=years);update(q)
    segment_cards(p)
    with st.expander('Modifier le tronçon actif',key='edit_segment'):
        with st.form('segment_meta_'+s['id']):
            name=st.text_input('Nom du tronçon',s['name']);a,b=st.columns(2)
            start=a.number_input('PK début (m)',0.,1e7,s['start']);end=b.number_input('PK fin (m)',0.,1e7,s['end'])
            alignment=st.selectbox('Tracé',['Alignement droit','Courbe'],index=['Alignement droit','Courbe'].index(s['alignment']))
            if st.form_submit_button('Enregistrer le tronçon'):
                q=copy.deepcopy(s);q.update(name=name,start=start,end=end,alignment=alignment);update(p,q)
    with st.expander('Ajouter un tronçon',key='add_segment'):
        with st.form('new_segment_'+p['id'],clear_on_submit=True):
            name=st.text_input('Nom du nouveau tronçon');a,b=st.columns(2);last=max(x['end'] for x in p['segments'])
            start=a.number_input('Nouveau PK début (m)',0.,1e7,last);end=b.number_input('Nouveau PK fin (m)',0.,1e7,last+1000)
            if st.form_submit_button('Ajouter ce tronçon'):
                q=copy.deepcopy(p);ss=new_segment(start,end,p['demo']);ss['name']=text(name,'Tronçon',100,True);q['segments'].append(ss);commit([q if x['id']==p['id'] else x for x in projects()],q['id'],ss['id']);st.rerun()
    with st.expander('Copie et suppression'):
        if st.button('Dupliquer cette route'):
            q=restore_bytes(backup_bytes([p]),st.session_state.demo)[0];commit(projects()+[q],q['id'],q['segments'][0]['id']);st.rerun()
        confirmed=st.checkbox('Confirmer la suppression de cette route et de ses tronçons')
        if st.button('Supprimer la route',disabled=not confirmed):
            rows=[x for x in projects() if x['id']!=p['id']];commit(rows)
            st.session_state.project_id=rows[0]['id'] if rows else None;st.session_state.segment_id=None;st.rerun()


def traffic_page():
    title('Trafic et exposition','Compter des passages, distinguer les tonnes et examiner la voie effectivement chargée.')
    p,s=require()
    if not p:return
    t=s['traffic']
    with st.form('traffic_'+s['id']):
        mode=st.selectbox('Mode de comptage',MODES,index=MODES.index(t['mode']))
        a,b=st.columns(2)
        daily=a.number_input('Poids lourds par jour avant répartition',0.,100000.,t['daily'])
        interval=b.number_input('Intervalle entre camions (minutes)',.1,1440.,t['interval'])
        hours=a.number_input('Heures de circulation par jour',.1,24.,t['hours']);days=b.number_input('Jours de circulation par an',1,365,t['days'])
        share=a.number_input('Part sur la voie étudiée (%)',.1,100.,t['share']);mass=b.number_input('Masse moyenne d’un camion (tonnes)',1.,150.,t['mass'])
        model=st.selectbox('Croissance du trafic',['Sans croissance','Linéaire','Géométrique'],index=['Sans croissance','Linéaire','Géométrique'].index(t['model']))
        growth=a.number_input('Croissance annuelle (%)',0.,15.,t['growth']);cam=b.number_input('CAM documenté (0 = inconnu)',0.,20.,t['cam'],help='Un CAM dépend du trafic et du type de chaussée. Aucune valeur n’est déduite de la masse totale.')
        source=st.text_area('Source / hypothèses du trafic',t['source'],max_chars=1000)
        st.caption('En mode fréquence, le champ PL/jour est ignoré. Sans croissance, le taux est ignoré. La part de voie s’applique une seule fois.')
        if st.form_submit_button('Enregistrer le trafic',type='primary'):
            q=copy.deepcopy(s);q['traffic'].update(mode=mode,daily=daily,interval=interval,hours=hours,days=days,share=share,mass=mass,model=model,growth=growth,cam=cam,source=source);update(p,q)
    frame=traffic_table(p,s);total=frame.iloc[-1,1];flow=daily_traffic(t)
    metrics([('Flux retenu',fmt(flow)+' PL/j','Après répartition sur la voie'),('Cumul projeté',fmt(total,0),'Passages, pas tonnes'),('Masse cumulée indicative',fmt(total*t['mass']/1e6,3)+' Mt','Pas une capacité de chaussée'),('Essieux équivalents',fmt(total*t['cam'],0) if t['cam'] else 'Non calculés','NPL × CAM si documenté')])
    chart(go.Figure(go.Scatter(x=frame.iloc[:,0],y=frame.iloc[:,1],mode='lines+markers',line=dict(color=TEAL))), 'Trafic cumulé par année sur la voie étudiée',key='traffic_curve')
    diagnostic_table(p,s,'Trafic')
    c=s['criteria'];note(f'Plage pédagogique évoquée « TC2 » : {fmt(c["traffic_low"],0)} à {fmt(c["traffic_high"],0)} PL cumulés. Le catalogue et ses conventions de bornes doivent être précisés. Un trafic inférieur à la plage n’est pas un défaut de résistance.')
    with st.expander('Calculatrice et exemples connus'):
        st.markdown('**Sans croissance :** N = flux de la voie × jours/an × années. **Linéaire :** facteur de cumul = n + g × n × (n − 1) / 2. **Géométrique :** somme de (1 + g)^k, k de 0 à n − 1 ; g est le taux en fraction.')
        st.markdown('Un camion toutes les 30 min pendant 24 h, 365 j/an, 20 ans, part 100 % : **350 400 PL**. Sur 14 h/jour : **204 400 PL**. Ces comptages ne sont pas automatiquement deux sens ni deux voies.')
    st.download_button('Trafic annuel CSV',csv_bytes(frame),'Route20Lab_trafic.csv','text/csv')


def soil_page():
    title('Plate-forme et CBR','Conserver les essais individuels et repérer le point observé le plus faible.')
    p,s=require()
    if not p:return
    note(f'Critère du projet : <b>CBR strictement supérieur à {fmt(s["criteria"]["cbr"])}</b>. Le minimum observé pilote le contrôle ; une moyenne élevée ne masque pas un essai faible.')
    with st.form('add_cbr_'+s['id'],clear_on_submit=True):
        a,b=st.columns(2);pk=a.number_input('PK de l’essai (m)',s['start'],s['end'],(s['start']+s['end'])/2);cbr=b.number_input('Indice CBR',0.,300.,18.)
        condition=a.selectbox('Conditions de l’essai',['Après immersion','Non immergé','In situ','Non précisé'])
        when=b.date_input('Date de l’essai',date.today());source=st.text_input('Référence de l’essai / laboratoire',max_chars=200)
        if st.form_submit_button('Ajouter cet essai',type='primary'):
            q=copy.deepcopy(s);q['soil'].append(dict(pk=pk,cbr=cbr,condition=condition,date=when.isoformat(),source=source));update(p,q)
    if s['soil']:
        df=pd.DataFrame(s['soil']);st.dataframe(df,hide_index=True,alt='Essais CBR individuels avec PK, conditions, date et source')
        fig=go.Figure(go.Scatter(x=df.pk,y=df.cbr,mode='markers',marker=dict(size=12,color=TEAL),text=df.condition));fig.add_hline(y=s['criteria']['cbr'],line_dash='dash',line_color=RED);fig.update_xaxes(title='PK (m)');fig.update_yaxes(title='Indice CBR')
        chart(fig,'CBR observés par point et seuil strict du projet',key='soil_chart')
        with st.expander('Corriger ou retirer un essai',key='edit_cbr'):
            idx=st.selectbox('Essai à corriger',range(len(s['soil'])),format_func=lambda i:f"{i+1} · PK {fmt(s['soil'][i]['pk'])} · CBR {fmt(s['soil'][i]['cbr'])}")
            with st.form('edit_cbr_form_'+s['id']+'_'+str(idx)):
                val=st.number_input('CBR corrigé',0.,300.,s['soil'][idx]['cbr'])
                if st.form_submit_button('Enregistrer la correction'):
                    q=copy.deepcopy(s);q['soil'][idx]['cbr']=val;update(p,q)
            remove=st.checkbox('Confirmer le retrait de cet essai')
            if st.button('Retirer cet essai',disabled=not remove):q=copy.deepcopy(s);q['soil'].pop(idx);update(p,q)
    with st.expander('Importer des essais CSV / Excel',key='import_cbr'):
        template=pd.DataFrame([dict(pk=(s['start']+s['end'])/2,cbr=18,condition='Après immersion',date='2026-10-01',source='Exemple fictif à remplacer')])
        st.download_button('Modèle CBR CSV',csv_bytes(template),'modele_cbr.csv','text/csv')
        upload=st.file_uploader('Fichier CBR',type=['csv','xlsx'],key='cbr_upload_'+s['id'])
        if upload:
            try:
                rows=import_cbr(upload.getvalue(),upload.name.rsplit('.',1)[-1],s);st.dataframe(pd.DataFrame(rows),hide_index=True,alt='Aperçu des essais CBR avant remplacement')
                confirmed=st.checkbox('Remplacer les essais de ce tronçon par ce fichier')
                if st.button('Importer les essais vérifiés',disabled=not confirmed):q=copy.deepcopy(s);q['soil']=rows;update(p,q)
            except ValueError as exc:st.error(str(exc))
    diagnostic_table(p,s,'Plate-forme')
    st.caption('Les mesures sont déclarées par l’utilisateur. Les zones non testées et les différences d’état hydrique ne sont pas automatiquement qualifiées.')


def water_page():
    title('Eau, fossés et buses','Vérifier les cotes du projet et distinguer géométrie et justification hydraulique.')
    p,s=require()
    if not p:return
    w=s['water']
    with st.form('water_'+s['id']):
        a,b=st.columns(2);data={}
        for k,label,lo,hi in [('cross_left','Pente transversale gauche (%)',0.,15.),('cross_right','Pente transversale droite (%)',0.,15.),('cant','Dévers en courbe (%)',0.,15.),('shoulder_left','Pente totale accotement gauche (%)',0.,15.),('shoulder_right','Pente totale accotement droit (%)',0.,15.)]:
            data[k]=(a if len(data)%2==0 else b).number_input(label,lo,hi,w[k],step=.1)
        data['shape']=a.selectbox('Forme des fossés',['Trapézoïdal','Rectangulaire','Triangulaire','Absent'],index=['Trapézoïdal','Rectangulaire','Triangulaire','Absent'].index(w['shape']))
        data['lining']=b.text_input('Revêtement du fossé',w['lining'],max_chars=80)
        for i,(k,label,lo,hi,step) in enumerate([('bottom','Largeur au fond du fossé (m)',.1,5.,.05),('depth','Profondeur du fossé (m)',.1,3.,.05),('side','Talus du fossé H/V',.1,3.,.1),('slope','Pente longitudinale du fossé (%)',.01,15.,.1),('outlet','Espacement maximal des exutoires (m)',1.,5000.,10.),('diameter','Diamètre intérieur des buses (mm)',100.,5000.,100.),('spacing','Espacement maximal des buses (m)',1.,5000.,10.)]):
            data[k]=(a if i%2==0 else b).number_input(label,lo,hi,w[k],step=step)
        data['outlet_clear']=st.checkbox('Exutoires libres et accessibles',w['outlet_clear']);data['culvert_clear']=st.checkbox('Buses et entrées / sorties dégagées',w['culvert_clear'])
        data['source']=st.text_area('Source des dimensions et observations',w['source'],max_chars=1000)
        data['hydraulic_ref']=st.text_area('Référence de l’étude hydraulique (facultative si absente)',w['hydraulic_ref'],max_chars=1000)
        if st.form_submit_button('Enregistrer le drainage',type='primary'):
            q=copy.deepcopy(s);q['water'].update(data);update(p,q)
    chart(cross_section(s),'Coupe transversale et fossés illustratifs',key='water_cut')
    st.caption('Le dévers est contrôlé seulement pour les tronçons en courbe ; les deux pentes transversales le sont en alignement droit. Les buses doivent être positionnées selon le relief et les écoulements, pas uniquement selon un espacement.')
    diagnostic_table(p,s,'Drainage')
    st.subheader('Repérage longitudinal indicatif')
    fig=go.Figure(go.Scatter(x=[s['start'],s['end']],y=[0,0],mode='lines',line=dict(color=INK,width=14),name='Tronçon'))
    for spacing,y,label,color in [(w['spacing'],0,'Repères buses',TEAL),(w['outlet'],-1,'Repères exutoires','#8A9A2D')]:
        positions=np.arange(s['start']+spacing,s['end'],spacing)
        if len(positions)>1000:positions=positions[:1000]
        fig.add_trace(go.Scatter(x=positions,y=np.full(len(positions),y),mode='markers',marker=dict(size=13,color=color,symbol='diamond'),name=label,hovertemplate='PK %{x:.0f} m<extra>'+label+'</extra>'))
    fig.update_xaxes(title='PK (m)');fig.update_yaxes(visible=False,range=[-1.6,.8])
    chart(fig,'Repères réguliers indicatifs des buses et exutoires selon les espacements saisis',key='drainage_plan',height=250)
    st.caption('Schéma de repérage, sans altimétrie. Repères intérieurs réguliers, hors extrémités ; ce n’est ni un plan d’implantation hydraulique ni un décompte des ouvrages nécessaires. Affichage limité à 1 000 repères par type.')
    with st.expander('Calcul hydraulique indicatif du fossé',key='hydraulic'):
        st.caption('Manning-Strickler, écoulement uniforme dans un fossé trapézoïdal. Ne calcule ni le débit de pluie ni la capacité des buses ni les remous.')
        with st.form('ditch_capacity_'+s['id']):
            a,b=st.columns(2);depth=a.number_input('Hauteur d’eau de calcul (m)',.01,3.,w['water_depth'],step=.05)
            rough=b.number_input('Rugosité de Manning n',.01,.15,w['roughness'],step=.005,format='%.3f')
            flow=a.number_input('Débit demandé au fossé (m³/s ; 0 = inconnu)',0.,100.,w['flow'],step=.05)
            if st.form_submit_button('Calculer l’indicateur hydraulique'):
                q=copy.deepcopy(s);q['water'].update(water_depth=depth,roughness=rough,flow=flow);update(p,q)
        cap=ditch_capacity(w)
        if cap:
            st.markdown(f"Section mouillée : **{fmt(cap['area'],3)} m²** · capacité théorique : **{fmt(cap['flow'],3)} m³/s**.")
            st.caption('Q = (1/n) × A × (A/P)^(2/3) × √I, avec I en fraction. A = (b + z × y) × y ; P = b + 2y√(1 + z²).')
            if w['flow']>0:
                st.info('Sous ces seules hypothèses : '+('capacité calculée supérieure ou égale au débit demandé.' if cap['flow']>=w['flow'] else 'capacité calculée inférieure au débit demandé.'))
        else:st.caption('Cet indicateur est disponible uniquement pour un fossé trapézoïdal.')
    with st.expander('Mesurer une pente simplement'):
        st.markdown('**Pente (%) = dénivelé / distance horizontale × 100.** Sur 2 m, 2,5 % correspondent à **5 cm**. Utilisez une mesure de niveau et une distance ; une bouteille qui roule ne mesure pas une pente. Faites les observations hors circulation.')


def structure_page():
    title('Structure de chaussée','Comparer des épaisseurs à une référence et identifier les études encore nécessaires.')
    p,s=require()
    if not p:return
    b=s['structure']
    with st.form('structure_'+s['id']):
        a,bb=st.columns(2);data={}
        for i,(k,label) in enumerate([('lanes','Largeur de chaussée (m)'),('shoulder_left','Largeur accotement gauche (m)'),('shoulder_right','Largeur accotement droit (m)'),('declared','Largeur de plate-forme déclarée (m ; 0 = inconnue)')]):
            data[k]=(a if i%2==0 else bb).number_input(label,0. if k=='declared' else .1,50.,b[k],step=.1)
        st.caption('Plate-forme calculée = chaussée + deux accotements, hors fossés et bandes supplémentaires non modélisées.')
        layers=[]
        for i,l in enumerate(b['layers']):
            st.markdown('**'+l['name']+'**');a,bb=st.columns(2)
            mat=st.text_input('Matériau · '+l['name'],l['material'],key='mat_'+s['id']+str(i),max_chars=100)
            thick=a.number_input('Épaisseur mesurée (cm) · '+l['name'],.1,150.,l['thickness'],step=1.)
            ref=bb.number_input('Épaisseur de référence (cm) · '+l['name'],.1,150.,l['reference'],step=1.)
            layers.append(dict(name=l['name'],material=mat,thickness=thick,reference=ref))
        data['source']=st.text_area('Source des épaisseurs et matériaux',b['source'],max_chars=1000)
        data['design_ref']=st.text_area('Référence de la note de dimensionnement',b['design_ref'],max_chars=1000)
        data['quality_ref']=st.text_area('Référence des contrôles matériaux / compactage',b['quality_ref'],max_chars=1000)
        if st.form_submit_button('Enregistrer la structure',type='primary'):
            q=copy.deepcopy(s);q['structure'].update(data,layers=layers);update(p,q)
    metrics([('Épaisseur totale',fmt(sum(l['thickness'] for l in b['layers']),0)+' cm','Somme des quatre couches'),('Chaussée',fmt(b['lanes'])+' m','Largeur roulable saisie'),('Plate-forme calculée',fmt(b['lanes']+b['shoulder_left']+b['shoulder_right'])+' m','Hors fossés'),('Plate-forme déclarée',fmt(b['declared'])+' m' if b['declared'] else 'Inconnue','À réconcilier avec les cotes')])
    chart(cross_section(s),'Couches de la chaussée avec matériaux et épaisseurs renseignées',key='structure_cut')
    diagnostic_table(p,s,'Structure')
    note('La coupe reçue propose <b>6 / 20 / 25 / 30 cm</b>. Ce sont des valeurs d’exemple. Le dessin indique 12 m de plate-forme, alors que 7 + 1,5 + 1,5 = 10 m. Respecter une épaisseur ne vérifie pas la fatigue, l’orniérage ni la qualité du matériau.')


@st.cache_data(ttl=1800,max_entries=6,show_spinner=False)
def cached_simulation(payload,years,count,seed,flow,growth):return simulate_traffic(json.loads(payload),years,count,seed,flow,growth)


def simulation_page():
    title('Simulations du trafic','Des scénarios d’exposition au trafic, sans probabilité inventée de durée de vie.')
    p,s=require()
    if not p:return
    raw=daily_traffic(s['traffic'])/(s['traffic']['share']/100)
    with st.form('sim_'+s['id']):
        a,b=st.columns(2);bounds=[]
        for col,label,values,hi in [(a,'PL par jour avant répartition',[raw*.8,raw,raw*1.2],100000.),(b,'Croissance annuelle (%)',[0.,1.,3.],15.)]:
            with col:
                st.subheader(label)
                bounds.append(tuple(st.number_input(label+' · '+lab,0.,hi,min(hi,float(v)),key='sim_'+lab+label+s['id']) for lab,v in zip(['minimum','mode','maximum'],values)))
        count=st.selectbox('Nombre de tirages',[1000,10000,50000,100000],index=1);seed=st.number_input('Graine aléatoire',0,2**32-1,42)
        st.caption('Une loi triangulaire pour le flux initial et une pour la croissance, indépendantes. La part de voie et le modèle de croissance viennent du trafic enregistré. En mode sans croissance, les bornes de croissance sont ignorées.')
        if st.form_submit_button('Lancer les simulations',type='primary',icon=':material/play_arrow:'):
            payload=json.dumps(s,sort_keys=True)
            with st.spinner('Calcul des scénarios de trafic…'):frame,bands=cached_simulation(payload,p['years'],count,seed,bounds[0],bounds[1])
            st.session_state.simulation=dict(payload=payload,years=p['years'],count=count,seed=seed,bounds=bounds,frame=frame,bands=bands)
    r=st.session_state.simulation
    if not r or r['payload']!=json.dumps(s,sort_keys=True) or r['years']!=p['years']:
        note('Lancez le calcul pour le tronçon enregistré. Après modification des données, les anciens résultats sont retirés.');return
    frame,bands=r['frame'],r['bands'];hits=int(frame.depassement.sum());low,high=wilson(hits,len(frame))
    metrics([('Dépassement du plafond',fmt(hits/len(frame)*100,1)+' %',f"{hits} / {len(frame)} scénarios"),('Cumul médian',fmt(frame.PL_cumules.median(),0),'PL sur la voie étudiée'),('Quantile 95 %',fmt(frame.PL_cumules.quantile(.95),0),'95 % des cumuls en dessous'),('Intervalle Wilson 95 %',fmt(low*100,3)+'–'+fmt(high*100,3)+' %','Précision de la fréquence simulée')])
    st.caption(f"Résultat affiché : {r['count']} tirages, graine {r['seed']}, flux {r['bounds'][0]}, croissance {r['bounds'][1]}. Ces hypothèses sont celles du dernier lancement.")
    fig=go.Figure();fig.add_trace(go.Scatter(x=bands.Année,y=bands.P05,line=dict(width=0),name='P05'));fig.add_trace(go.Scatter(x=bands.Année,y=bands.P95,fill='tonexty',fillcolor='rgba(8,127,131,.15)',line=dict(width=0),name='Bande P05–P95'));fig.add_trace(go.Scatter(x=bands.Année,y=bands.Médiane,name='Médiane',line=dict(color=TEAL,width=3)));fig.add_hline(y=s['criteria']['traffic_high'],line_dash='dash',line_color=RED);fig.update_xaxes(title='Année');fig.update_yaxes(title='Poids lourds cumulés')
    chart(fig,'Bande des cumuls simulés et plafond de trafic par année',key='sim_band')
    fig=go.Figure(go.Histogram(x=frame.PL_cumules,nbinsx=35,marker_color=TEAL));fig.add_vline(x=s['criteria']['traffic_high'],line_dash='dash',line_color=RED);fig.update_xaxes(title='PL cumulés à l’horizon');fig.update_yaxes(title='Nombre de scénarios');chart(fig,'Histogramme du trafic cumulé simulé',key='sim_hist')
    st.caption('L’année de premier dépassement vaut 0 dans le CSV si aucun dépassement ne survient dans l’horizon. Ce code ne signifie pas une rupture à l’année 0. Les cumuls sont des projections moyennes, possiblement décimales ; aucune panne de chaussée n’est simulée.')
    st.subheader('Sensibilité : un calcul complémentaire')
    base=float(cumulative(bounds_mean(r['bounds'][0])*s['traffic']['share']/100,bounds_mean(r['bounds'][1]),p['years'],s['traffic']['days'],s['traffic']['model']))
    labels=['Flux initial +10 %','Flux initial −10 %','Croissance +1 point'];flow0=bounds_mean(r['bounds'][0])*s['traffic']['share']/100;g0=bounds_mean(r['bounds'][1])
    deltas=[float(cumulative(flow0*f,g,p['years'],s['traffic']['days'],s['traffic']['model']))-base for f,g in [(1.1,g0),(.9,g0),(1.,min(15.,g0+1))]]
    chart(go.Figure(go.Bar(x=deltas,y=labels,orientation='h',marker_color=[TEAL,TEAL,'#A4B234'])),'Sensibilité déterministe du cumul aux moyennes des lois, référence séparée de la médiane simulée',key='sim_sensitivity',height=280)
    st.caption('Ce complément utilise les moyennes des lois triangulaires, pas le coût ni la durée de service. L’intervalle de Wilson ne couvre pas l’erreur du modèle ou des données.')
    st.download_button('Tirages de trafic CSV',csv_bytes(frame),'Route20Lab_simulations.csv','text/csv')
    meta={k:v for k,v in r.items() if k not in ['frame','bands','payload']};meta.update(app='Route20Lab',segment=json.loads(r['payload']),project_years=p['years'],demo=p['demo'])
    st.download_button('Hypothèses de simulation JSON',json.dumps(meta,ensure_ascii=False,indent=2).encode(),'Route20Lab_hypotheses.json','application/json')


def bounds_mean(b):return sum(b)/3


def maintenance_page():
    title('Entretien et interventions','Préparer les campagnes et consigner ce qui a réellement été réalisé.')
    p,s=require()
    if not p:return
    a=s['maintenance'];o=s['observation']
    with st.form('maintenance_'+s['id']):
        left,right=st.columns(2)
        hole=left.number_input('Profondeur maximale observée des nids-de-poule (cm)',0.,100.,o['pothole'],step=.5)
        threshold=right.number_input('Seuil de réparation du projet (cm)',.1,20.,a['pothole'],step=.5)
        blocked=st.checkbox('Accotements en bourrelet / faisant obstacle à l’eau',o['shoulder_blocked'])
        width=left.number_input('Largeur de débroussaillage par côté (m)',.1,10.,a['brush_width'],step=.1)
        review=right.number_input('Intervalle de revue du revêtement (ans)',1,20,a['review'])
        m1=left.number_input('Mois de curage n°1',1,12,a['month1']);m2=right.number_input('Mois de curage n°2',1,12,a['month2'])
        dc=left.number_input('Coût prévu par campagne de curage (FCFA)',0.,1e9,a['drain_cost'],step=1000.)
        bc=right.number_input('Coût prévu du débroussaillage annuel (FCFA)',0.,1e9,a['brush_cost'],step=1000.)
        notes=st.text_area('Observations de terrain',o['notes'],max_chars=1000)
        st.caption('Les mois initiaux 3 et 10 sont des hypothèses de calendrier, pas des saisons locales établies. Les coûts à zéro sont non chiffrés. Débroussaillage annuel et revue à 7 ans sont des propositions modifiables.')
        if st.form_submit_button('Enregistrer l’entretien',type='primary'):
            q=copy.deepcopy(s);q['observation'].update(pothole=hole,shoulder_blocked=blocked,notes=notes);q['maintenance'].update(pothole=threshold,brush_width=width,review=review,month1=m1,month2=m2,drain_cost=dc,brush_cost=bc);update(p,q)
    st.subheader('Actions à prioriser')
    if o['pothole']>=a['pothole']:st.warning('Nid-de-poule au seuil du projet : organiser une inspection et une réparation adaptée, avec recherche de la cause.')
    else:st.caption('Nids-de-poule : surveillance ; aucun dépassement du seuil saisi n’est déclaré.')
    if o['shoulder_blocked']:st.warning('Prévoir le dérasement des accotements pour rétablir l’écoulement, après examen du profil et de la stabilité des rives.')
    if not s['water']['outlet_clear'] or not s['water']['culvert_clear']:st.warning('Dégager les ouvrages obstrués et inspecter les dommages.')
    issues=[r for r in diagnose(p,s) if r['statut']=='bad']
    if issues:st.caption(f'{len(issues)} écart(s) de diagnostic restent à traiter en parallèle de l’entretien courant.')
    note(f'Curage des fossés et buses : <b>deux campagnes par an</b>, à positionner avant et après les périodes pluvieuses locales. Débroussaillage : <b>{fmt(a["brush_width"])} m de chaque côté</b>. À <b>{a["review"]} ans</b>, examiner l’état et la pertinence d’un enduit ; il n’est pas prescrit automatiquement.')
    plan=maintenance_plan(p,s);st.subheader('Calendrier sur l’horizon de l’étude')
    year=st.selectbox('Année à afficher',sorted({int(x[:4]) for x in plan.date}))
    st.dataframe(plan[plan.date.str.startswith(str(year))],hide_index=True,alt='Interventions prévues et réalisées pour l’année choisie')
    with st.expander('Enregistrer ou corriger une réalisation',key='edit_event'):
        event_id=st.selectbox('Intervention du calendrier',plan.id.tolist(),format_func=lambda i:next(r['date']+' · '+r['action'] for r in plan.to_dict('records') if r['id']==i))
        old=next((x for x in a['events'] if x['id']==event_id),dict(done=False,cost=0.,date=date.today().isoformat(),note=''))
        with st.form('event_'+s['id']+event_id):
            done=st.checkbox('Intervention effectivement réalisée',old['done']);when=st.date_input('Date de réalisation / de saisie',date.fromisoformat(old['date']))
            cost=st.number_input('Coût total constaté (FCFA)',0.,1e10,old['cost'],step=1000.);memo=st.text_input('Note de réalisation',old['note'],max_chars=600)
            if st.form_submit_button('Enregistrer cette intervention'):
                if when>date.today():raise ValueError('Une réalisation constatée ne peut être datée dans le futur.')
                if not done and cost>0:raise ValueError('Un coût constaté nécessite une intervention déclarée réalisée.')
                q=copy.deepcopy(s);q['maintenance']['events']=[x for x in a['events'] if x['id']!=event_id]+[dict(id=event_id,date=when.isoformat(),done=done,cost=cost,note=memo)];update(p,q)
    financial=plan.assign(annee_prevue=plan.date.str[:4]).groupby('annee_prevue',as_index=False).cout_prevu.sum()
    actual=pd.DataFrame([dict(annee_reelle=x['date'][:4],cout_constate=x['cost']) for x in a['events'] if x['done']],columns=['annee_reelle','cout_constate'])
    actual=actual.groupby('annee_reelle',as_index=False).cout_constate.sum()
    fig=go.Figure(go.Bar(x=financial.annee_prevue,y=financial.cout_prevu,name='Prévu (hors interventions non chiffrées)',marker_color=TEAL))
    fig.add_trace(go.Bar(x=actual.annee_reelle,y=actual.cout_constate,name='Constaté par année de réalisation',marker_color='#A3AE32'));fig.update_layout(barmode='group');fig.update_xaxes(type='category',title='Année');fig.update_yaxes(title='FCFA')
    chart(fig,'Comparaison des dépenses prévues et constatées d’entretien par année',key='maintenance_cost')
    st.caption('Une correction d’intervention remplace la précédente saisie. Si le calendrier change, les anciennes réalisations restent dans le graphe des dépenses constatées, la sauvegarde et l’onglet Realisations d’Excel, même sans rendez-vous correspondant.')
    if a['events']:
        with st.expander('Historique de toutes les réalisations'):
            st.dataframe(pd.DataFrame(a['events']),hide_index=True,alt='Historique complet des saisies de réalisation, y compris hors calendrier actuel')
    st.download_button('Calendrier complet CSV',csv_bytes(plan),'Route20Lab_entretien.csv','text/csv')


def criteria_form(p,s):
    c=s['criteria']
    with st.form('criteria_'+s['id']):
        a,b=st.columns(2);values={}
        labels=[('cbr','Seuil CBR strict'),('traffic_low','Borne basse de comparaison (PL)'),('traffic_high','Plafond de trafic du projet (PL)'),('cross','Pente transversale cible (%)'),('tolerance','Tolérance transversale (points de %)'),('cant_min','Dévers minimum (%)'),('cant_max','Dévers maximum (%)'),('shoulder_min','Pente totale accotements minimum (%)'),('shoulder_max','Pente totale accotements maximum (%)'),('bottom','Largeur minimale au fond (m)'),('depth_min','Profondeur minimale fossé (m)'),('depth_max','Profondeur maximale fossé (m)'),('slope_min','Pente longitudinale minimale (%)'),('slope_max','Pente longitudinale maximale (%)'),('outlet','Espacement maximal exutoires (m)'),('diameter','Diamètre minimal buses (mm)'),('spacing','Espacement maximal buses (m)')]
        for i,(k,label) in enumerate(labels):values[k]=(a if i%2==0 else b).number_input(label,value=float(c[k]),min_value=0.,max_value=1e8)
        values['shoulder_total']=st.checkbox('Pour cette étude, 3–4 % désigne la pente totale des accotements',c['shoulder_total'])
        values['reference']=st.text_area('Origine et portée des critères',c['reference'],max_chars=1000)
        if st.form_submit_button('Enregistrer les critères'):
            q=copy.deepcopy(s);q['criteria'].update(values);update(p,q)


def reports_page():
    title('Rapports, données et méthode','Emporter le diagnostic et conserver les hypothèses pour reprendre l’étude.')
    p,s=require()
    if p:
        prefix='FICTIF_' if p['demo'] else ''
        with st.container(horizontal=True):
            st.download_button('Rapport de diagnostic PDF',pdf_bytes(p,s),prefix+'Route20Lab_diagnostic.pdf','application/pdf',icon=':material/picture_as_pdf:')
            st.download_button('Classeur Excel',excel_bytes(p,s),prefix+'Route20Lab_projet.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',icon=':material/table_view:')
            st.download_button('Diagnostic CSV',csv_bytes(pd.DataFrame(diagnose(p,s))),prefix+'Route20Lab_diagnostic.csv','text/csv')
        st.caption('Exports du tronçon actif enregistré. Les simulations se téléchargent dans leur rubrique ; le PDF ne les inclut pas automatiquement.')
    st.download_button('Sauvegarde complète JSON',backup_bytes(projects()),'Route20Lab_sauvegarde.json','application/json',icon=':material/save:')
    with st.expander('Restaurer une sauvegarde',key='restore'):
        with st.form('restore_form'):
            upload=st.file_uploader('Sauvegarde Route20Lab JSON',type=['json']);confirmed=st.checkbox('Ajouter des copies sans supprimer les routes existantes')
            if st.form_submit_button('Importer les copies'):
                if upload is None:raise ValueError('Choisissez d’abord une sauvegarde.')
                if not confirmed:raise ValueError('Confirmez l’ajout de copies.')
                rows=restore_bytes(upload.getvalue(),st.session_state.demo);commit(projects()+rows,rows[0]['id'],rows[0]['segments'][0]['id']);st.rerun()
    if p:
        with st.expander('Critères du projet : examiner ou modifier',key='criteria'):criteria_form(p,s)
    with st.expander('Comprendre les ambiguïtés de la coupe',expanded=False):
        st.markdown('• **Exutoires : 200 m** confirmé par l’utilisateur.\n• **Dérasement des accotements** confirmé pour faciliter l’écoulement.\n• **3–4 % d’accotement** : interprétation en pente totale à confirmer pour chaque étude.\n• **Plate-forme : 10 m calculés**, alors que l’image mentionne 12 m ; tout autre composant doit être décrit.\n• **Fond de fossé : 0,50 m** retenu d’après le texte, contre 0,40 m sur l’image.\n• **1000 mm et 250 m pour les buses** : critères du projet, pas une vérification hydraulique.\n• **6/20/25/30 cm** : couches de l’illustration, pas une structure universelle.')
    st.subheader('Parcours de test')
    st.markdown('1. Chargez l’exemple fictif : **350 400 PL** sur 20 ans.\n2. Trafic : passez à **14 h/jour** ; attendez **204 400 PL**, puis revenez à 24 h.\n3. CBR : ajoutez un essai à **15** ; le seuil strict doit être en écart.\n4. Drainage : passez la largeur au fond à **0,40 m** ; un écart doit apparaître.\n5. Structure : déclarez **12 m** de plate-forme ; la somme calculée reste **10 m**.\n6. Simulation : flux **48/48/48**, croissance **0/0/0**, 1 000 tirages : **350 400 PL dans tous les scénarios**.\n7. Entretien : déclarez **2 cm** de nid-de-poule et un accotement bloqué ; les deux interventions sont proposées.\n8. Exportez PDF, Excel et JSON. Restaurez le JSON dans l’espace fictif pour ajouter une copie.')
    st.subheader('Méthode et sources')
    st.caption('Les contrôles portent sur les données déclarées et les critères choisis. Les références d’études ne sont pas expertisées. Aucune durée de vie mécanique, conformité réglementaire ou garantie de 20 ans n’est calculée. Un état favorable ne supprime pas le besoin de vérifier matériaux, fatigue, déformation, hydrologie et exécution.')
    for label,url in SOURCES.items():st.link_button(label,url)
    st.caption('Stockage SQLite hors du dossier livré. Sans comptes, le registre d’un serveur partagé est commun. Sur Cloud, le disque peut être temporaire : conserver des sauvegardes JSON. Une démonstration est isolée du registre réel.')


def main():
    st.set_page_config(page_title='Route20Lab · Routes et entretien',page_icon=':material/add_road:',layout='wide')
    ss=st.session_state
    if 'projects' not in ss:
        try:ss.projects,ss.revision=load_store()
        except (ValueError,sqlite3.Error,OSError) as exc:st.error('Impossible d’ouvrir le registre : '+str(exc));st.stop()
        ss.update(demo=False,demo_projects=[],project_id=ss.projects[0]['id'] if ss.projects else None,segment_id=None,simulation=None)
    st.html(CSS)
    st.html('<div class="mast"><div class="wordmark">ROUTE<b>20</b>LAB <span style="font-weight:400;font-size:.75rem">/ ÉTUDE & ENTRETIEN</span></div><span class="pill">'+('EXEMPLE FICTIF' if ss.demo else 'DONNÉES DU PROJET')+'</span></div>')
    global PAGES
    PAGES=[st.Page(fn,title=label,icon=':material/'+icon+':',url_path=slug,default=i==0) for i,(fn,label,icon,slug) in enumerate([
        (dashboard,'Vue d’ensemble','dashboard','accueil'),(project_page,'Routes et tronçons','add_road','routes'),(traffic_page,'Trafic','local_shipping','trafic'),(soil_page,'Plate-forme et CBR','landscape','sol'),(water_page,'Eau et drainage','water_drop','eau'),(structure_page,'Structure','layers','structure'),(simulation_page,'Simulations','query_stats','simulations'),(maintenance_page,'Entretien','construction','entretien'),(reports_page,'Rapports et méthode','description','rapports')])]
    with st.sidebar:
        st.html('<div class="side-logo">ROUTE<b>20</b>LAB</div><div class="side-desc">Observer. Comprendre. Entretenir.</div>')
        st.button('Charger l’exemple fictif',on_click=demo,icon=':material/science:',width='stretch')
        if ss.demo:st.button('Revenir à mes routes',on_click=personal,icon=':material/folder:',width='stretch')
        p,s=active()
        if p:st.caption('Route active : '+p['name']+'\n\nTronçon : '+s['name'])
    page=st.navigation(PAGES,position='sidebar')
    with st.sidebar:
        if not ss.demo and st.button('Recharger le registre',icon=':material/refresh:'):
            ss.projects,ss.revision=load_store();ss.project_id=ss.projects[0]['id'] if ss.projects else None;ss.segment_id=None;ss.simulation=None;st.rerun()
        st.caption('Version '+VERSION+' · Objectif de service : 20 ans minimum')
    if ss.get('flash'):st.success(ss.pop('flash'))
    try:page.run()
    except (ValueError,sqlite3.Error,OSError,KeyError) as exc:st.error('L’action n’a pas abouti : '+str(exc))
    st.html('<div class="foot">Route20Lab · Critères du projet et données déclarées. L’objectif de 20 ans reste à justifier par les études ; aucune durée de vie garantie n’est calculée.</div>')


if __name__=='__main__':main()
