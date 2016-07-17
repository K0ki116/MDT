from mdt.models.compartments import CompartmentConfig, CLCodeFromInlineString

__author__ = 'Francisco.Lagos'

#From protocol, if the text_message_signal is SE, we can setup TM = 0 in all the volumes, which returns to the standard SE text_message_signal decay
"""" Generalised STEAM equation.
This equation can be used to calculate relaxation time (T1/T2) from spin echo (SE) and/or stimulated spin echo (STE) data. It is important to notice that
in the protocol has to define some parameters in an arbitrary way:
(1) For SE data, the original equation contains only the first refocusing pulse variable, but half of this value and in the power of two (sin(Refoc_fa1/2)**2).
For that it is needed to define Refoc_fa2 = Refoc_fa1 and Refoc_fa1 has to be HALF of the used FA in the protocol. Also, the 0.5 factor is not included, then
SEf (Spin echo flag) should be 0. Finally, TM (mixing time) has to be 0.
(2) For STE data, this equation is used totally. Just SEf = 1.
"""""

class ExpT1ExpT2STEAM(CompartmentConfig):

    name = 'ExpT1ExpT2STEAM'
    cl_function_name = 'cmExpT1ExpT2STEAM'
    parameter_list = ('SEf', 'TM', 'TE', 'flip_angle', 'Refoc_fa1', 'Refoc_fa2', 'T1', 'T2')
    cl_code = CLCodeFromInlineString("""
        return pow(0.5, SEf) * sin(flip_angle) * sin(Refoc_fa1) * sin(Refoc_fa2) * exp(-TE / T2) * exp(-TM / T1);
    """)
