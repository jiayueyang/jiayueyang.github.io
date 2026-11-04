/*
 * Browser-based black hole visualization, inspired by NASA SVS 13326.
 * Schwarzschild null geodesics are integrated for primary and secondary
 * disk images; rotating plasma & Doppler emission are phenomenological.
 * This is NOT an exact Kerr spacetime geodesic ray-tracing calculation.
 * Works without packages; the homepage keeps its Canvas 2D fallback.
 */
(function(global){
"use strict";

const vertex=`
attribute vec2 a_position;
void main(){gl_Position=vec4(a_position,0.0,1.0);}
`;
const fragment=`
precision highp float;
uniform vec2 u_resolution;
uniform float u_time;
uniform float u_spin;
#define PI 3.141592653589793
#define M 0.16
#define CRIT 0.83138439
float hash(vec2 p){
  p=fract(p*vec2(127.1,311.7));
  p+=dot(p,p+19.19);
  return fract(p.x*p.y);
}
float noise(vec2 p){
  vec2 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);
  return mix(mix(hash(i),hash(i+vec2(1.,0.)),f.x),
    mix(hash(i+vec2(0.,1.)),hash(i+vec2(1.,1.)),f.x),f.y);
}
float turbulence(vec2 p){
  return .54*noise(p)+.27*noise(p*2.13+13.2)+.19*noise(p*4.17+37.0);
}
vec3 diskEmission(float r,float azi,float order){
  float rin=1.0,rout=3.65;
  if(r<rin||r>rout)return vec3(0.0);
  float speed=min(.69,sqrt(M/max(.50,r-2.0*M)));
  float gamma=1.0/sqrt(1.0-speed*speed);
  float dop=1.0/(gamma*(1.0+u_spin*speed*.94*cos(azi)));
  float grav=sqrt(max(.05,1.0-2.0*M/r));
  float redshift=dop*grav;
  float ang=azi-u_time*.37/pow(r,1.5)*u_spin;
  vec2 flow=vec2(ang*9.0+1.2*sin(r*9.0),r*22.0);
  float fractal=turbulence(flow+vec2(2.1*turbulence(flow*.4),0.0));
  float strands=pow(.5+.5*sin(r*73.0+ang*19.0+fractal*6.5),2.0);
  float lanes=pow(.5+.5*sin(r*27.0-ang*6.0+fractal*3.0),2.0);
  float knots=smoothstep(.56,.79,fractal);
  float plasma=(.24+.62*fractal+.23*strands+.27*knots)*(.63+.37*lanes);
  float edge=smoothstep(rin,rin+.17,r)*(1.0-smoothstep(rout-.32,rout,r));
  float intensity=pow(rin/r,1.46)*edge*plasma*pow(redshift,3.7);
  float heat=clamp((1.7/r)*redshift*.83,0.0,1.0);
  vec3 color=mix(vec3(.38,.03,.007),vec3(1.0,.30,.043),
    smoothstep(.12,.63,heat));
  color=mix(color,vec3(1.0,.81,.43),smoothstep(.57,.97,heat));
  float imageWeight=order<.5?2.25:(order<1.5?.80:.26);
  return color*intensity*imageWeight;
}
void main(){
  vec2 pixel=gl_FragCoord.xy-.5*u_resolution;
  float scale=min(u_resolution.x/5.6,u_resolution.y/4.25);
  vec2 imagePos=pixel/scale-vec2(.044*u_spin,0.0);
  float b=length(imagePos);
  if(b>3.75){gl_FragColor=vec4(0.0);return;}
  float inc=1.23,sini=sin(inc),cosi=cos(inc);
  float safeB=max(b,.005);
  float phi0=max(.001,atan(cosi,sini*imagePos.y/safeB));
  vec3 emission=vec3(0.0);
  if(b>CRIT*.97){
    float du=1.0/safeB,u=0.0,phi=0.0;
    float target=phi0,order=0.0,step=.052;
    // Schwarzschild ray equation u'' + u = 3 M u^2
    for(int j=0;j<174;j++){
      float acc=-u+3.0*M*u*u;
      du+=.5*step*acc;
      float oldU=u;
      u+=step*du;
      du+=.5*step*(-u+3.0*M*u*u);
      phi+=step;
      if(phi>=target&&order<3.0){
        float crossU=mix(oldU,u,clamp((target-(phi-step))/step,0.0,1.0));
        if(crossU>.0001){
          float r=1.0/crossU;
          float parity=mod(order,2.0)<.5?1.0:-1.0;
          float xx=sin(target)*imagePos.x/safeB;
          float yy=cos(target)*sini+sin(target)*imagePos.y*cosi/safeB;
          float azi=atan(parity*yy,parity*xx);
          azi+=u_spin*.082/(r*r);
          emission+=diskEmission(r,azi,order);
        }
        order+=1.0;target+=PI;
      }
      if(u>1.0/(2.0*M)||(u<-.001&&phi>2.9))break;
    }
  }
  float az=atan(imagePos.y,imagePos.x);
  float beam=.27+.73*pow(.5-.5*u_spin*cos(az),1.6);
  float rings=exp(-pow((b-CRIT-.015)/.013,2.0))*.22;
  rings+=exp(-pow((b-CRIT-.048)/.027,2.0))*.075;
  rings+=exp(-pow((b-CRIT+.008)/.005,2.0))*.12;
  emission+=vec3(1.0,.46,.12)*rings*beam;
  float haze=exp(-pow((b-1.07)/.77,2.0))*.025*(.6+.4*beam);
  emission+=vec3(1.0,.24,.045)*haze;
  emission=vec3(1.0)-exp(-max(emission,vec3(0.0))*1.42);
  emission=pow(emission,vec3(.92));
  float shadow=1.0-smoothstep(CRIT-.045,CRIT+.016,b);
  float alpha=clamp(max(shadow,max(emission.r,max(emission.g,emission.b))*.86),0.0,1.0);
  vec3 rgb=mix(emission,vec3(.001,.001,.004),shadow);
  gl_FragColor=vec4(rgb,alpha);
}
`;

function compile(gl,type,source){
  const shader=gl.createShader(type);
  gl.shaderSource(shader,source);gl.compileShader(shader);
  if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS)){
    const error=gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw Error(error||"GPU shader compilation failed");
  }
  return shader;
}
function createBlackHoleRaytracer(options){
  const mobile=Boolean(options&&options.mobile);
  const canvas=document.createElement("canvas");
  canvas.width=mobile?192:304;
  canvas.height=mobile?144:228;
  const gl=canvas.getContext("webgl",{
    alpha:true,premultipliedAlpha:false,antialias:false,
    preserveDrawingBuffer:true,powerPreference:"low-power"
  });
  if(!gl)return null;
  let program;
  try{
    program=gl.createProgram();
    const vs=compile(gl,gl.VERTEX_SHADER,vertex);
    const fs=compile(gl,gl.FRAGMENT_SHADER,fragment);
    gl.attachShader(program,vs);gl.attachShader(program,fs);
    gl.linkProgram(program);
    gl.deleteShader(vs);gl.deleteShader(fs);
    if(!gl.getProgramParameter(program,gl.LINK_STATUS))
      throw Error(gl.getProgramInfoLog(program)||"GPU shader link failed");
    const buffer=gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
    gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([
      -1,-1,1,-1,-1,1,-1,1,1,-1,1,1
    ]),gl.STATIC_DRAW);
    const a=gl.getAttribLocation(program,"a_position");
    gl.enableVertexAttribArray(a);
    gl.vertexAttribPointer(a,2,gl.FLOAT,false,0,0);
  }catch(error){
    console.warn("Using black-hole Canvas fallback:",error);
    return null;
  }
  const res=gl.getUniformLocation(program,"u_resolution");
  const clock=gl.getUniformLocation(program,"u_time");
  const spin=gl.getUniformLocation(program,"u_spin");
  gl.viewport(0,0,canvas.width,canvas.height);
  gl.useProgram(program);
  gl.uniform2f(res,canvas.width,canvas.height);
  gl.disable(gl.DEPTH_TEST);
  gl.disable(gl.BLEND);
  let last=-1e6;
  return {
    canvas,
    render(time,orientation){
      if(time-last<1/(mobile?12:20))return canvas;
      last=time;
      if(gl.isContextLost())return canvas;
      gl.uniform1f(clock,time);
      gl.uniform1f(spin,orientation);
      gl.clearColor(0,0,0,0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES,0,6);
      return canvas;
    }
  };
}
global.createBlackHoleRaytracer=createBlackHoleRaytracer;
})(window);
