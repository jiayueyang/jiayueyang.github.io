# The Hawking temperature of a black hole

PaperTranslate

# 黑洞的霍金温度

远处的观测者如何确定黑洞的温度？对于史瓦西黑洞，表面引力决定无穷远处的温度，引力红移则决定热平衡时静止观测者测得的局域温度。

## 视界与表面引力

考虑四维时空中一个质量为 M、不带电且不旋转的黑洞。取光速、约化普朗克常数和玻尔兹曼常数均为一，保留牛顿常数 G。

选取史瓦西时间坐标，使其在无穷远处与固有时间一致。度规函数在视界半径处为零。

\begin{equation}
f(r)=1-\frac{2GM}{r},\qquad r_{\mathrm{h}}=2GM\label{eq:metric}
\end{equation}

对于这个度规，度规函数在视界处对半径的导数的一半就是表面引力。

\begin{equation}
\kappa=\frac{1}{2}f'(r_{\mathrm{h}})=\frac{GM}{r_{\mathrm{h}}^{2}}=\frac{1}{4GM}\label{eq:kappa}
\end{equation}

## 无穷远处的温度

霍金的量子场计算给出了无穷远处出射粒子的热占据因子中的温度 \cite{hawking1975}。外部几何对波的散射还会给观测到的辐射通量带来一个依赖频率的透射因子。

\begin{equation}
T_{\mathrm{H}}=\frac{\kappa}{2\pi}=\frac{1}{8\pi GM}\label{eq:temperature}
\end{equation}

牛顿常数固定时，质量加倍，霍金温度减半。

## 有限半径处的温度

现在考虑视界外的静态热平衡态 \cite{tolman1930}。静止观测者测得的温度取决于其固有时钟相对于无穷远处时钟的走时速率。

\begin{equation}
T_{\mathrm{loc}}(r)\sqrt{f(r)}=T_{\mathrm{H}},\qquad r>r_{\mathrm{h}}\label{eq:redshift}
\end{equation}

越靠近视界，静止时钟走得越慢，相应的局域平衡温度也越高。在视界上无法保持静止。蒸发过程中，探测器的完整响应还取决于量子态和轨迹。

## References

S. W. Hawking, Particle creation by black holes, Communications in Mathematical Physics 43, 199–220 (1975), Sec. 2. DOI 10.1007/BF02345020.

R. C. Tolman and P. Ehrenfest, Temperature Equilibrium in a Static Gravitational Field, Physical Review 36, 1791–1798 (1930), abstract. DOI 10.1103/PhysRev.36.1791.
