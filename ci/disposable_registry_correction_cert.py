import os,json,tempfile,uuid,hashlib
from pathlib import Path
from datetime import timedelta
from sqlalchemy import create_engine,select,func,text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session,sessionmaker
from dish_pg import models,stage3_models as wf
from dish_pg.bootstrap import bootstrap_initial_generation
from dish_pg.command_port import CommandCall,PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.read_model import PostgresReadModel
from dish_pg.repositories import registry_source_import_run,REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,REGISTRY_ROLE_CORRECTION_KIND
from dish_pg.workflow import WorkflowAuthorityService,RequestSpec,ExecutionSpec
from dish_pg.repositories import AuthorityRepository,RegistryRepository,CoreAuthorityError
from tests.postgresql import test_initial_bootstrap as b
from tests.support.postgresql.core import _reset_postgresql_schema

DSN=os.environ['DISH_TEST_POSTGRESQL_DSN']; NOW=b.NOW
RG='1217084794163035'; VG='1217091890481531'
RI=uuid.uuid5(uuid.NAMESPACE_URL,'asana-section:'+RG); VI=uuid.uuid5(uuid.NAMESPACE_URL,'asana-section:'+VG)

def db():
 _reset_postgresql_schema(DSN); e=create_engine(DSN,future=True); assert e.dialect.name=='postgresql'; return sessionmaker(bind=e,class_=Session,autoflush=False,expire_on_commit=False,future=True),e

def main():
 f,e=db(); p=Path(tempfile.mkdtemp())
 try:
  src=b._source(p,b._record(uuid.uuid4(),'cert-r',section_id=RI,section_gid=RG,section_name='Research Queue'),b._record(uuid.uuid4(),'cert-v',section_id=VI,section_gid=VG,section_name='Verification Queue'))
  spec=b._spec(src)
  with session_scope(f) as s: boot=bootstrap_initial_generation(s,spec,clock=lambda:NOW)
  with f() as s:
   es=list(s.scalars(select(models.SectionRegistryEntry).where(models.SectionRegistryEntry.registry_version_id==boot.registry_version_id))); before={x.section_id:x.workflow_role for x in es}; assert before[RI]=='imported-section-'+RG and before[VI]=='imported-section-'+VG
   pred=s.get(models.SectionRegistryVersion,boot.registry_version_id); pred_sha=pred.registry_sha256
  run=uuid.uuid4(); req=uuid.uuid4(); args={'research_queue_section_id':str(RI),'verification_queue_section_id':str(VI)}
  with session_scope(f) as s:
   WorkflowAuthorityService(s).register_run(run_id=run,generation_id=boot.generation_id,owner_id='Marco',agent='marco',capability_digest=b'c'*32,registered_at=NOW)
   r=PostgresCommandPort(s,cursor_secret=b'x'*32).execute(CommandCall(command_name='revise-section-registry',arguments=args,owner_id='Marco',principal_class='admin',run_id=run,request_id=req,now=NOW,protocol_release=spec.honest.protocol_version)); assert r.ok and r.data['changed']; out=dict(r.data)
  with f() as s:
   a=s.get(models.ActiveSectionRegistry,boot.generation_id); assert a.registry_revision==2 and a.registry_version_id!=boot.registry_version_id
   v=s.get(models.SectionRegistryVersion,a.registry_version_id); c=s.get(models.ImportRun,uuid.UUID(out['correction_import_run_id'])); assert v.version_number==2 and c.status=='complete' and c.source_release==REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE and c.provenance['correction_kind']==REGISTRY_ROLE_CORRECTION_KIND and c.provenance['source_record_count']==0 and c.provenance['source_import_run_id']==str(boot.import_run_id) and c.provenance['predecessor_registry_version_id']==str(boot.registry_version_id) and c.provenance['requested_roles']=={'research_queue':str(RI),'verification_queue':str(VI)} and c.provenance['result_registry_sha256']==v.registry_sha256
   x=s.get(wf.CommandExecution,uuid.UUID(c.provenance['command_execution_id'])); q=s.get(wf.ServiceRequest,x.request_id); assert x.status=='committed' and x.command_name=='revise-section-registry' and q.request_id==req and q.principal_class=='admin' and q.command_name=='revise-section-registry' and q.canonical_payload['arguments']==args and registry_source_import_run(s,v).import_run_id==boot.import_run_id
   act=s.get(models.SectionRegistryActivation,a.registry_activation_id); assert act.activation_route=='import' and act.import_run_id==c.import_run_id and act.command_execution_id is None
   rows=s.execute(select(models.SectionRegistryEntry,models.SectionExternalAlias.external_id).join(models.SectionExternalAlias,models.SectionExternalAlias.section_id==models.SectionRegistryEntry.section_id).where(models.SectionRegistryEntry.registry_version_id==a.registry_version_id,models.SectionExternalAlias.external_system=='asana',models.SectionExternalAlias.state=='active')).all(); m={z.workflow_role:g for z,g in rows}; assert m['research_queue']==RG and m['verification_queue']==VG
   old={z.section_id:z.workflow_role for z in s.scalars(select(models.SectionRegistryEntry).where(models.SectionRegistryEntry.registry_version_id==boot.registry_version_id))}; assert old==before and s.get(models.SectionRegistryVersion,boot.registry_version_id).registry_sha256==pred_sha
   rm={z['workflow_role']:z['section_gid'] for z in PostgresReadModel(s,cursor_secret=b'x'*32).sections()}; assert rm['research_queue']==RG and rm['verification_queue']==VG
  with session_scope(f) as s:
   rr=PostgresCommandPort(s,cursor_secret=b'x'*32).execute(CommandCall(command_name='revise-section-registry',arguments=args,owner_id='Marco',principal_class='admin',run_id=run,request_id=req,now=NOW,protocol_release=spec.honest.protocol_version)); assert rr.ok and rr.request_replayed and rr.data['registry_version_id']==out['registry_version_id']
  with session_scope(f) as s:
   rr=PostgresCommandPort(s,cursor_secret=b'x'*32).execute(CommandCall(command_name='revise-section-registry',arguments=args,owner_id='Marco',principal_class='admin',run_id=run,request_id=uuid.uuid4(),now=NOW+timedelta(seconds=1),protocol_release=spec.honest.protocol_version)); assert rr.ok and rr.data['changed'] is False and rr.data['registry_version_id']==out['registry_version_id']
  with f() as s:
   assert s.scalar(select(func.count()).select_from(models.SectionRegistryVersion).where(models.SectionRegistryVersion.generation_id==boot.generation_id))==2; assert s.scalar(select(func.count()).select_from(models.SectionRegistryActivation).where(models.SectionRegistryActivation.generation_id==boot.generation_id))==2
  imm=[]
  for n,sql,pa in [('entry',"update section_registry_entries set workflow_role='tampered' where registry_version_id=:v and section_id=:s",{'v':str(boot.registry_version_id),'s':str(RI)}),('version','delete from section_registry_versions where registry_version_id=:v',{'v':out['registry_version_id']}),('activation','update section_registry_activations set registry_revision=999 where registry_activation_id=:a',{'a':out['registry_activation_id']})]:
   try:
    with e.begin() as c: c.execute(text(sql),pa)
   except DBAPIError as ex: assert 'immutable' in str(ex.orig).lower(); imm.append(n)
   else: raise AssertionError('immutable probe '+n)
  ar=uuid.uuid4()
  with session_scope(f) as s:
   WorkflowAuthorityService(s).register_run(run_id=ar,generation_id=boot.generation_id,owner_id='cert-agent',agent='cert-agent',capability_digest=b'a'*32,registered_at=NOW+timedelta(seconds=2)); cr=PostgresCommandPort(s,cursor_secret=b'x'*32).execute(CommandCall(command_name='create',arguments={'title':'[ready] Native registry certification','body':'cert body'},owner_id='cert-agent',principal_class='agent',run_id=ar,request_id=uuid.uuid4(),now=NOW+timedelta(seconds=2),protocol_release=spec.honest.protocol_version)); assert cr.ok; tid=uuid.UUID(cr.data['task_id'])
  with f() as s:
   pl=s.get(models.CurrentTaskSectionPlacement,(boot.generation_id,tid)); assert pl.section_id==RI
  return {'predecessor':str(boot.registry_version_id),'result_version':out['registry_version_id'],'activation':out['registry_activation_id'],'correction_import':out['correction_import_run_id'],'roles':{'research_queue':RG,'verification_queue':VG},'immutable':imm,'create_research_gid':RG}
 finally: e.dispose()

def states():
 f,e=db(); p=Path(tempfile.mkdtemp())
 try:
  src=b._source(p,b._record(uuid.uuid4(),'s1'),b._record(uuid.uuid4(),'s2',section_id=b.OTHER_SECTION_ID,section_gid=b.OTHER_SECTION_GID,section_name='Verification Queue')); spec=b._spec(src)
  from dataclasses import replace
  from dish_pg.bootstrap import apply_research_queue_role
  spec=replace(spec,sections=apply_research_queue_role(spec.sections,research_queue_section_id=b.DEFAULT_SECTION_ID))
  with session_scope(f) as s: boot=bootstrap_initial_generation(s,spec,clock=lambda:NOW)
  with session_scope(f) as s:
   g=s.get(models.AuthorityGeneration,boot.generation_id); pred=s.get(models.SectionRegistryVersion,boot.registry_version_id); run,req,exe=uuid.uuid4(),uuid.uuid4(),uuid.uuid4(); args={'research_queue_section_id':str(b.DEFAULT_SECTION_ID),'verification_queue_section_id':str(b.OTHER_SECTION_ID)}; payload={'command':'revise-section-registry','arguments':args,'owner_id':'Marco','run_id':str(run)}; w=WorkflowAuthorityService(s); w.register_run(run_id=run,generation_id=boot.generation_id,owner_id='Marco',agent='marco',capability_digest=b'c'*32,registered_at=NOW); w.admit_request(RequestSpec(request_id=req,generation_id=boot.generation_id,run_id=run,owner_id='Marco',principal_class='admin',command_name='revise-section-registry',canonical_payload=payload,protocol_release=spec.honest.protocol_version,dish_release=g.dish_release,admitted_at=NOW)); x=w.begin_execution(ExecutionSpec(execution_id=exe,request_id=req,generation_id=boot.generation_id,task_id=None,operation_id=None,command_name='revise-section-registry',transaction_profile='L',canonical_intent=payload,pinned_inputs={'now':NOW.isoformat()},contract_binding_id=boot.binding_id,admitted_at=NOW)); w.repo.claim_execution(execution_id=exe,claimant=f'Marco:{run}',claim_token=uuid.uuid4(),now=NOW,ttl=timedelta(minutes=2)); s.refresh(x); assert x.status=='claimed'; srcimp=registry_source_import_run(s,pred); vid=uuid.uuid4(); h='a'*64; roles={'research_queue':str(b.DEFAULT_SECTION_ID),'verification_queue':str(b.OTHER_SECTION_ID)}; cp={'format':'dish-registry-role-correction-v1','generation_id':str(boot.generation_id),'predecessor_registry_version_id':str(pred.registry_version_id),'source_import_run_id':str(srcimp.import_run_id),'command_execution_id':str(exe),'requested_roles':roles,'result_registry_sha256':h}; ch=hashlib.sha256(json.dumps(cp,sort_keys=True,separators=(',',':')).encode()).hexdigest(); ci=uuid.uuid4(); AuthorityRepository(s).add_import_run(models.ImportRun(import_run_id=ci,source_commit=srcimp.source_commit,source_release=REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,legacy_generation_id=srcimp.legacy_generation_id,baseline_high_water_mark=f'registry-role-correction:{pred.registry_version_id}:{h}',source_bundle_sha256=ch,status='complete',started_at=NOW,completed_at=NOW,provenance={'correction_kind':REGISTRY_ROLE_CORRECTION_KIND,'correction_bundle_sha256':ch,'source_import_run_id':str(srcimp.import_run_id),'predecessor_registry_version_id':str(pred.registry_version_id),'command_execution_id':str(exe),'requested_roles':roles,'result_registry_sha256':h,'source_record_count':0})); pe=list(s.scalars(select(models.SectionRegistryEntry).where(models.SectionRegistryEntry.registry_version_id==pred.registry_version_id).order_by(models.SectionRegistryEntry.ordinal))); cv=models.SectionRegistryVersion(registry_version_id=vid,generation_id=boot.generation_id,version_number=pred.version_number+1,import_run_id=ci,contract_binding_id=pred.contract_binding_id,registry_sha256=h,created_at=NOW); RegistryRepository(s).add_registry_version(cv,[models.SectionRegistryEntry(registry_version_id=vid,section_id=z.section_id,ordinal=z.ordinal,display_name=z.display_name,workflow_role=('research_queue' if z.section_id==b.DEFAULT_SECTION_ID else 'verification_queue' if z.section_id==b.OTHER_SECTION_ID else z.workflow_role)) for z in pe])
   for st in ['claimed','failed','uncertain','cancelled']:
    if st!='claimed': x.status=st; x.claim_owner=x.claim_token=x.claim_expires_at=None; x.terminal_at=NOW; x.execution_revision+=1; s.flush()
    try: registry_source_import_run(s,cv)
    except CoreAuthorityError as ex: assert 'command execution provenance' in str(ex)
    else: raise AssertionError(st+' established durable provenance')
  return {x:'rejected' for x in ['claimed','failed','uncertain','cancelled']}
 finally:e.dispose()

print('REGISTRY_CORRECTION_CERTIFICATION='+json.dumps({'status':'PASS','main':main(),'durable_states':states()},sort_keys=True,separators=(',',':')))
