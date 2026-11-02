import * as THREE from "three";

export function modelMaterial(surface) {
  const color = { iron: 0x9da5ae, coil: 0xbd733e, magnet: 0x6d78b3 }[surface.kind];
  const material = new THREE.MeshStandardMaterial({ color, metalness: .15, roughness: .55, side: THREE.DoubleSide });
  const pole = surface.pole;
  if (!pole) return material;
  const rotation = { value: 0 };
  material.userData.poleRotation = rotation;
  material.color.set(0xffffff);
  material.onBeforeCompile = shader => {
    Object.assign(shader.uniforms, {
      north: { value: new THREE.Color(0xcf4548) }, south: { value: new THREE.Color(0x356ec6) },
      poleMode: { value: pole.rule === "plane" ? 1 : 2 },
      poleVector: { value: new THREE.Vector3(...(pole.vector || [0, 0, 0])) },
      poleCenter: { value: new THREE.Vector3(...(pole.center || [0, 0, 0])) },
      poleRadius: { value: pole.radius || 0 }, poleSign: { value: pole.sign || 1 },
      poleCount: { value: pole.count || 0 }, polePhase: { value: pole.phase || 0 },
      polePeriod: { value: pole.period }, poleStart: { value: pole.start },
      poleRotation: rotation,
    });
    shader.vertexShader = "varying vec3 polePosition;\n" + shader.vertexShader;
    shader.vertexShader = shader.vertexShader.replace("#include <begin_vertex>", "#include <begin_vertex>\npolePosition=(modelMatrix*vec4(position,1.0)).xyz;");
    shader.fragmentShader = "varying vec3 polePosition; uniform vec3 north,south,poleVector,poleCenter; uniform float poleMode,poleRadius,poleSign,poleCount,polePhase,polePeriod,poleStart,poleRotation;\n" + shader.fragmentShader;
    shader.fragmentShader = shader.fragmentShader.replace("#include <color_fragment>", `
      #include <color_fragment>
      vec3 polePoint=polePosition;
      polePoint.xy=mat2(cos(poleRotation),-sin(poleRotation),sin(poleRotation),cos(poleRotation))*polePoint.xy;
      float poleSide;
      if(poleMode < 1.5) {
        vec3 p=polePoint;
        if(polePeriod > 0.0) {
          float a=floor((atan(p.y,p.x)-poleStart)/polePeriod)*polePeriod;
          p.xy=mat2(cos(a),-sin(a),sin(a),cos(a))*p.xy;
        }
        poleSide=dot(p-poleCenter,poleVector);
      } else {
        float polarity=poleSign;
        if(poleCount > 0.0) {
          float a=atan(polePoint.y,polePoint.x)-polePhase+3.141592653589793/poleCount;
          float k=floor(mod(a,6.283185307179586)/(6.283185307179586/poleCount));
          polarity*=1.0-2.0*mod(k,2.0);
        }
        poleSide=(length(polePoint.xy)-poleRadius)*polarity;
      }
      diffuseColor.rgb=mix(south,north,step(0.0,poleSide));
    `);
  };
  material.customProgramCacheKey = () => "magnet-poles";
  return material;
}
