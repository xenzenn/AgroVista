"""Admin-only dispatch; viewers receive successful data updates automatically."""
import hmac
import time
import logging
import streamlit as st
from cloud_updates import latest_update,sync_database,start_refresh,run_status
from cloud_runtime import QUERY_LOCK

def settings():
    try:return dict(st.secrets.get('cloud_refresh',{}))
    except (FileNotFoundError,KeyError):return {}

@st.cache_data(ttl=300,max_entries=1,show_spinner=False)
def published_update():
    return latest_update()

def sync_if_enabled(database):
    config=settings()
    if not config.get('enabled',False):return
    if time.time()<st.session_state.get('_cloud_retry_after',0):
        notice=st.session_state.get('_cloud_update_failure')
        if notice:st.warning(notice)
        return
    try:
        manifest=published_update()
        if manifest:
            changed=sync_database(database,manifest,lock=QUERY_LOCK)
            if changed:st.toast('Dashboard data updated successfully.')
        st.session_state.pop('_cloud_update_failure',None)
        st.session_state.pop('_cloud_retry_after',None)
    except Exception as exc:
        # A failed update does not prevent the last known good dashboard from rendering.
        logging.exception('AgroVista update rejected; saved database retained')
        detail=(str(exc)[:220] if isinstance(exc,ValueError) else type(exc).__name__)
        notice='Could not load the latest data update. Showing the previous saved data. '+detail
        st.session_state['_cloud_update_failure']=notice
        st.session_state['_cloud_retry_after']=time.time()+60
        st.warning(notice)

def admin_panel():
    config=settings()
    if not config.get('enabled',False):return
    with st.expander('Data updates · Admin'):
        st.caption('Successful published updates are checked automatically every five minutes while the app is in use.')
        password=st.text_input('Admin password',type='password',key='cloud_admin_password')
        expected=str(config.get('admin_password',''))
        if len(expected)<16 or expected.startswith(('REPLACE_','GANTI_')):
            st.info('Set your own admin password of at least 16 characters in Streamlit Secrets.')
            return
        if not expected or not password or not hmac.compare_digest(password.encode(),expected.encode()):
            if password:st.caption('Password not accepted.')
            return
        token=str(config.get('github_token',''))
        if not token:
            st.info('Add the GitHub Actions token to Streamlit secrets before starting a refresh.')
            return
        if st.button('Update data',key='cloud_start_refresh',type='primary'):
            if time.time()-st.session_state.get('_last_cloud_dispatch',0)<60:
                st.info('A refresh was requested recently. Check its status below.')
            else:
                try:
                    result=start_refresh(token)
                    st.session_state['_last_cloud_dispatch']=time.time()
                    st.success('Refresh already running.' if result['already_running'] else 'Refresh requested. The dashboard stays available while it runs.')
                    st.link_button('View refresh progress',result['html_url'])
                except Exception as exc:st.error(str(exc))
        if st.button('Check refresh status / load published data',key='cloud_check_refresh'):
            try:
                status=run_status(token)
                if status:
                    st.write('Status:',status['status'],'·',status.get('conclusion') or 'running / queued')
                    st.link_button('Open refresh log',status['html_url'])
                else:st.info('No workflow runs yet. Commit the workflow to main first.')
                published_update.clear()
                st.session_state.pop('_cloud_retry_after',None)
                st.session_state['_cloud_sync_notice']=True
                st.rerun()
            except Exception as exc:st.error(str(exc))
        if st.session_state.pop('_cloud_sync_notice',False):
            try:
                status=run_status(token)
                if status:
                    st.write('Latest refresh:',status['status'],'·',status.get('conclusion') or 'not finished')
                    st.link_button('View latest refresh',status['html_url'])
            except Exception as exc:st.caption(str(exc))

OVERVIEW_REPAIR_VERSION=2
