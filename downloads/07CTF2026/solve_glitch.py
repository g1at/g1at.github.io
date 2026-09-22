"""Recover the GLITCH CTF flag from the original ELF without executing it.
Usage: python solve_glitch.py /path/to/glitch
Dependency: pycryptodome (python -m pip install pycryptodome)
The four inputs and observation transcripts were recovered using IDA MCP.
All encrypted stages, the root record and the final flag are authenticated.
"""
import sys,json,hashlib,struct
from pathlib import Path
from Crypto.Cipher import ChaCha20_Poly1305
STAGES = [('70a6c3735621d60b000000803a381fdb', '70a6c3735621d60b00000080c6c7e024040000008400000000000000'), ('7b66dcb437621462d154d5e0ed76b33fb2452076600a71f6', '9cf4e47ee79883469cf4e47ee79883469cf4e47ee79883461000000000000000'), ('b27bd5237630fd7f', 'b27bd523763005004500000000000000000000800aec60'), ('832183fb039eb0c8e44e002e132b2cf993', '42804000000000009380400000000000935680a2cd38b696adee27b07986a4ddf4')]

def unlock_stages(data):
    data=bytearray(data)
    previous=b''
    for stage,(input_hex,transcript_hex) in enumerate(STAGES,1):
        key=hashlib.sha256(f'GLITCH/STAGE{stage+1}'.encode()+previous+bytes.fromhex(input_hex)+bytes.fromhex(transcript_hex)).digest()
        manifest=[0x430700,0x430560,0x4303c0,0x430220][stage-1]-0x400000
        version,number,count,_=struct.unpack_from('<4I',data,manifest)
        for idx in range(count):
            ent=bytes(data[manifest+16+48*idx:manifest+64+48*idx])
            addr,length,kind=struct.unpack('<QIB',ent[:13])
            aad=bytes(data[0x30890:0x308a0])+struct.pack('<4I',version,number,count,idx)+ent[:13]
            cipher=ChaCha20_Poly1305.new(key=key,nonce=ent[16:28]);cipher.update(aad)
            offset=addr-0x400000
            data[offset:offset+length]=cipher.decrypt_and_verify(bytes(data[offset:offset+length]),ent[28:44])
        print(f'Stage {stage}: {input_hex}')
        previous=key
    return bytes(data)

B=unlock_stages(Path(sys.argv[1] if len(sys.argv)>1 else 'glitch').read_bytes())
M=(1<<64)-1
def mem(a,n):return B[a-0x400000:a-0x400000+n]
def u64(b):return int.from_bytes(b,'little')
def rol(x,n):
 x&=M;n%=64
 return ((x<<n)|(x>>((64-n)%64)))&M
def ror(x,n):return rol(x,64-n)
def rol32(x,n):
 x&=0xffffffff;n%=32
 return ((x<<n)|(x>>((32-n)%32)))&0xffffffff
def ror32(x,n):return rol32(x,32-n)
def mix(n,x,y,z,d):
 n%=4;x&=M;y&=M;z&=M
 if n==0:
  t=rol(0xD9DB037C7AFF38F1*(((x^((d-0x195A78D3D244F8AA)&M))+(rol(y,33)^0xA685BC473EF7703E))^(0xF9AABC259A5E675B*z&M)),25)
  return ((t^(t>>15))+ror(d^z,15))&M
 if n==1:
  a=(x+0x4A475983876DD9CF*(z^0x74C01F9D17068D0D))&M
  b=y^a^ror(a+d,12)
  c=(ror(z,30)+0x02E454D955D520C1*(rol(b,12)^0x02618B9154F2FA7F))&M
  return c^(c>>10)
 if n==2:
  a=(0x64B0057CF3CE793B*(x^ror(y+0x2BC0F404CD214A40,15))+(d^0xBB07B4BD900F44A4))&M
  c=(0xD4DA7F5A8A1619BB*rol(a+(ror(a,7)^z),7))&M
  c^=c>>7
  return c^(c>>20)
 a=y^((0x4C2E1CAC53531A1B*d+0x65266733899D050D)&M)
 b=(0xFD2A43BD9862046D*((ror(x+a,7)^((z+0x44A2E541B7DBE5DA)&M))+rol(a^z,15)))&M
 c=rol(b,12)^b
 return c^(c>>12)

def tape():
 key=mem(0x427d20,16);state=list(struct.unpack('<4I',key));prev=key[0]
 for n in range(20):
  row=mem(0x419008+8*n,8);assert row[:2]==b'\x0f\x0b'
  out=[]
  for j,c in enumerate(row[2:]):
   out.append(c^prev);v=(c^(110*n+61*j))&255;prev=((v<<1)|(v>>7))&255
  op=out[0];v=int.from_bytes(bytes(out[1:5]),'little')
  assert out[5]==((n^key[n&15])^((sum(out[:5]))&255))
  idx=n&3
  if op==1:state[idx]=(state[idx]+v)&0xffffffff
  elif op==2:state[idx]^=v
  elif op==3:state[idx]=rol32(state[idx],v)
  elif op==4:state[idx]=(state[idx]*(v|1))&0xffffffff
  else:assert op==127 and n==19
 return hashlib.blake2s(b'GLITCH/STAGE5/TAPE'+struct.pack('<4I',*state)).digest()

CLASSES=mem(0x427cc0,32)
def build_pages():
 pages=bytearray(0x20000)
 seed=u64(mem(0x427c80,8))^u64(mem(0x427c88,8))
 for i in range(len(pages)):
  seed^=(seed<<13)&M;seed^=seed>>7;seed^=(seed<<17)&M
  pages[i]=(seed>>29)&255
 for i,v in enumerate(struct.unpack('<13520I',mem(0x41a940,13520*4))):
  x=((1783225927*((ror32(v,14)-i)&0xffffffff)+1460592784)&0xffffffff)^0xCC4FAED8
  pages[x&0x1ffff]=(x>>17)&255
 return pages
PAGES=build_pages()
TAPE=tape()

def round_detail(n,r,selector):
 tk=u64(TAPE[8*(n%4):8*(n%4)+8])
 kind=(TAPE[n]+5*selector+7*n)%3
 candidates=[i for i in range(32) if CLASSES[i]==kind]
 tag=selector|(n<<8)|(kind<<16)
 page=candidates[mix(n,r,tk,tag,0x4556415041474501)%len(candidates)]
 left,right=selector,n
 for j in range(6):
  shift=(2*j)&2
  mult=((24141>>(2*j%10))&30)|1
  add=(164+13*j+(24141>>(j+3)))&31
  f=(right*mult+add+((right>>(4-shift))|(right<<(shift+1))))&31
  left,right=right,left^f
 offset=4*(right|(left<<5))+((r^(r>>17)^TAPE[(n+1)%32])&3)
 packed=page|(offset<<8)|(kind<<24)|(selector<<32)
 if kind==0:value=PAGES[page*4096+offset]
 else:value=mix(n,r^tk,packed,tag,kind^0x4556414641554C02)&255
 rmix=mix(n,r,packed,value^tk^(kind<<56),0x455641524F554E03)
 share=mix(n,rmix,r,packed^value,0x4556415348415204)
 h=(rol(rmix^tk^r,7+n%23)+(rmix|1)*(tk|1))&M
 f=h^ror(h,11+n%17)
 return f,dict(n=n,r=r,page=page,offset=offset,kind=kind,value=value,tk=tk,packed=packed,tag=tag,mix=rmix,share=share)

def kdf48(domain,a,b,selector):
 return b''.join(hashlib.blake2s(domain+bytes([i])+a+b+bytes([selector])).digest() for i in range(2))[:48]
def target(selector):
 stored=bytes(mem(0x41a320+32*i+selector,1)[0] for i in range(16))
 t=kdf48(b'GLITCH/STAGE5/RELEASE/SHARD/TAPE',TAPE,b'',selector)
 u=kdf48(b'GLITCH/STAGE5/RELEASE/SHARD/TARGET/V2',TAPE,CLASSES,selector)
 return bytes(x^y^z for x,y,z in zip(stored,t,u))
def invert(selector):
 l,r=struct.unpack('<2Q',target(selector))
 final=(l,r)
 for n in reversed(range(32)):
  f,_=round_detail(n,l,selector)
  l,r=r^f,l
 a,b,c,d=struct.unpack('<4I',struct.pack('<2Q',l,r))
 keys=struct.unpack('<16I',mem(0x427ce0,64))
 for n in reversed(range(8)):
  oldb=ror32(a,11)^b
  oldc=(b-c-keys[2*n+1])&0xffffffff
  oldd=rol32(c,7)^d
  olda=(d-keys[2*n]-rol32(oldb,5))&0xffffffff
  a,b,c,d=olda,oldb,oldc,oldd
 return struct.pack('<4I',a,b,c,d),final

def forward(selector):
 l,r=struct.unpack('<2Q',target(selector))
 for n in reversed(range(32)):
  f,_=round_detail(n,l,selector);l,r=r^f,l
 th=hashlib.blake2s(b'GLITCH/STAGE5/TRANSCRIPT')
 sh=hashlib.blake2s(b'GLITCH/STAGE5/SHARES')
 for n in range(32):
  f,d=round_detail(n,r,selector)
  nr=l^f
  rec=struct.pack('<BBBBH5Q',n,d['page'],d['kind'],d['value'],d['offset'],r,d['mix'],r,nr,d['share'])
  th.update(rec);sh.update(struct.pack('<Q',d['share']))
  l,r=r,nr
 return struct.pack('<2Q',l,r),th.digest(),sh.digest()


def recover_flag():
    for selector in range(32):
        state,trace,shares=forward(selector)
        stored=bytes(mem(0x41a320+32*i+selector,1)[0] for i in range(16,48))
        tape_mask=kdf48(b'GLITCH/STAGE5/RELEASE/SHARD/TAPE',TAPE,b'',selector)[16:]
        walk_mask=kdf48(b'GLITCH/STAGE5/RELEASE/SHARD/WALK',trace,shares,selector)[16:]
        key_mask=hashlib.blake2s(b'GLITCH/STAGE5/KEY'+state+trace+shares+bytes([selector])).digest()
        key=bytes(a^b^c^d for a,b,c,d in zip(stored,tape_mask,walk_mask,key_mask))
        selection=hashlib.blake2s(b'GLITCH/STAGE6/SELECT/V1'+mem(0x427d30,16)+bytes([selector])+state+trace,digest_size=8).digest()
        record_idx=int.from_bytes(selection,'little')%3
        length=struct.unpack('<H',mem(0x41a140+record_idx*2,2))[0]
        rec=bytes(mem(0x41a160+record_idx+i*3,1)[0] for i in range(length))
        aad_len,ct_len=struct.unpack_from('<HH',rec,12)
        cipher=ChaCha20_Poly1305.new(key=key,nonce=rec[:12]);cipher.update(rec[16:16+aad_len])
        try:
            root=cipher.decrypt_and_verify(rec[16+aad_len:16+aad_len+ct_len],rec[16+aad_len+ct_len:])
        except ValueError:
            continue
        magic,version,flags=struct.unpack_from('<IHH',root)
        if (magic,version,flags)!=(0x35524c47,1,0):continue
        assert root[8:40]==mem(0x41a120,32) and struct.unpack_from('<I',root,40)[0]==35
        flag_key=hashlib.blake2s(b'GLITCH/FLAG/KEY/V1'+key+state+trace+shares+root[8:40]).digest()
        cipher=ChaCha20_Poly1305.new(key=flag_key,nonce=mem(0x41a100,12));cipher.update(mem(0x41a0c0,60))
        flag=cipher.decrypt_and_verify(mem(0x41a080,35),mem(0x41a070,16)).decode()
        assert flag.startswith('07CTF{') and flag.endswith('}')
        print('Stage 5 selector:',selector)
        print('All authentication tags verified.')
        print(flag)
        return flag
    raise RuntimeError('No valid flag branch')

if __name__=='__main__':
    recover_flag()
